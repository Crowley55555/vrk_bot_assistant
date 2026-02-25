"""
Умный парсер каталога ООО "Завод ВРК".

- Обходит страницы категорий, собирает ссылки на карточки товаров.
- Переходит на каждую карточку, извлекает полный набор данных.
- Реализует инкрементальное обновление (Delta Update):
  • новый товар → добавляет;
  • изменённый → перезаписывает;
  • без изменений → пропускает;
  • исчезнувший → помечает / удаляет (если SCRAPER_REMOVE_MISSING=True).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup, Tag
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import (
    BASE_SITE_URL,
    RAW_PRODUCTS_PATH,
    SCRAPER_MAX_RETRIES,
    SCRAPER_REQUEST_DELAY,
    SCRAPER_TIMEOUT,
    SCRAPER_REMOVE_MISSING,
    START_URLS,
)
from logger import get_logger
from models import Product

log = get_logger(__name__)

# ─── HTTP-клиент ────────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


@retry(
    stop=stop_after_attempt(SCRAPER_MAX_RETRIES),
    wait=wait_exponential(min=2, max=30),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    reraise=True,
)
async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    """Загружает HTML-страницу с повторами при ошибках сети."""
    resp = await client.get(url, headers=_HEADERS, timeout=SCRAPER_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


# ─── Утилиты ───────────────────────────────────────────────────────────────────

def _clean(text: str | None) -> str:
    """Убирает лишние пробелы и невидимые символы."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _content_hash(product: Product) -> str:
    """MD5 от ключевых полей — для отслеживания изменений."""
    blob = "|".join([
        product.name,
        product.price or "",
        product.description or "",
        json.dumps(product.characteristics, sort_keys=True, ensure_ascii=False),
    ])
    return hashlib.md5(blob.encode()).hexdigest()


def _abs_url(href: str) -> str:
    """Преобразует относительную ссылку в абсолютную."""
    if href.startswith("http"):
        return href
    return BASE_SITE_URL.rstrip("/") + "/" + href.lstrip("/")


# ─── Парсинг страницы категории ────────────────────────────────────────────────

def _parse_category_page(html: str, category_name: str) -> list[dict]:
    """
    Из страницы категории извлекает базовую информацию по карточкам:
    ссылку, название, артикул, цену, теги.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []

    cards = soup.select(
        ".product-card, "
        ".catalog-item, "
        "[class*='product'], "
        "[class*='card'], "
        "[data-product-id]"
    )

    if not cards:
        # Если CSS-селекторы не сработали, ищем по структуре ссылок
        links = soup.find_all("a", href=re.compile(r"/catalog/[^/]+/[^/]+"))
        seen: set[str] = set()
        for a_tag in links:
            href = _abs_url(a_tag.get("href", ""))
            if href in seen:
                continue
            seen.add(href)
            name = _clean(a_tag.get_text())
            if not name or len(name) < 3:
                continue
            items.append({"url": href, "name": name, "category": category_name})
        return items

    for card in cards:
        link_tag = card.find("a", href=re.compile(r"/catalog/"))
        if not link_tag:
            continue
        url = _abs_url(link_tag.get("href", ""))
        name = _clean(link_tag.get_text()) or _clean(card.find("h3", string=True) or "")

        art_tag = card.find(string=re.compile(r"(Арт\.|код:)\s*\d+"))
        article = ""
        if art_tag:
            m = re.search(r"(\d{3,})", str(art_tag))
            if m:
                article = m.group(1)

        price_tag = card.find(string=re.compile(r"\d[\d\s]*₽|Р"))
        price = _clean(str(price_tag)) if price_tag else None

        tags: list[str] = []
        for label in ("Хит", "Акция", "Советуем", "Новинка"):
            if card.find(string=re.compile(label)):
                tags.append(label)

        items.append({
            "url": url,
            "name": name,
            "article": article,
            "price": price,
            "category": category_name,
            "tags": tags,
        })

    return items


# ─── Парсинг страницы товара ───────────────────────────────────────────────────

def _parse_product_page(html: str, base_info: dict) -> Product:
    """
    Со страницы товара извлекает полное описание и характеристики.
    Дополняет базовую информацию из карточки категории.
    """
    soup = BeautifulSoup(html, "lxml")

    # Название (H1)
    h1 = soup.find("h1")
    name = _clean(h1.get_text()) if h1 else base_info.get("name", "")

    # Артикул
    article = base_info.get("article", "")
    if not article:
        art_el = soup.find(string=re.compile(r"(Артикул|Арт\.|код)"))
        if art_el:
            m = re.search(r"(\d{3,})", str(art_el.parent.get_text() if art_el.parent else art_el))
            if m:
                article = m.group(1)
    if not article:
        # Последний вариант — из URL
        m = re.search(r"-(\d+)$", base_info.get("url", ""))
        if m:
            article = m.group(1)

    # Описание — все параграфы внутри блока описания
    desc_parts: list[str] = []
    for section_title in ("Описание", "описание"):
        header = soup.find(string=re.compile(section_title, re.I))
        if header:
            container = header.find_parent(["div", "section", "article"])
            if container:
                for p in container.find_all(["p", "li"]):
                    txt = _clean(p.get_text())
                    if txt and len(txt) > 20:
                        desc_parts.append(txt)
                break
    if not desc_parts:
        for p in soup.select("div.product-description p, div.product-text p, .tab-content p"):
            txt = _clean(p.get_text())
            if txt and len(txt) > 20:
                desc_parts.append(txt)

    description = "\n".join(desc_parts) if desc_parts else None

    # Характеристики
    chars: dict[str, str] = {}
    char_section = soup.find(string=re.compile(r"Характеристики", re.I))
    if char_section:
        parent = char_section.find_parent(["div", "section", "table"])
        if parent:
            rows = parent.find_all(["tr", "li", "div"])
            for row in rows:
                cells = row.find_all(["td", "th", "span", "dt", "dd"])
                if len(cells) >= 2:
                    key = _clean(cells[0].get_text())
                    val = _clean(cells[1].get_text())
                    if key and val and key.lower() != "характеристики":
                        chars[key] = val
    if not chars:
        for keyword in ("Место применения", "Материал", "Конструкция", "Регулировка", "Форма"):
            el = soup.find(string=re.compile(keyword))
            if el:
                parent = el.find_parent()
                if parent:
                    txt = _clean(parent.get_text())
                    parts = txt.split(keyword, 1)
                    if len(parts) == 2:
                        val = parts[1].strip(" —:–\t\n")
                        if val:
                            chars[keyword] = val

    # Цена
    price = base_info.get("price")
    if not price:
        price_el = soup.find(string=re.compile(r"\d[\d\s]*[₽Р]"))
        if price_el:
            price = _clean(str(price_el))

    # Старая цена
    old_price: Optional[str] = None
    old_tag = soup.find(class_=re.compile(r"old.?price|price.?old|crossed"))
    if old_tag:
        old_price = _clean(old_tag.get_text())

    product = Product(
        article=article or hashlib.md5(base_info["url"].encode()).hexdigest()[:8],
        name=name,
        url=base_info["url"],
        price=price,
        old_price=old_price,
        description=description,
        category=base_info.get("category"),
        characteristics=chars,
        tags=base_info.get("tags", []),
    )
    product.content_hash = _content_hash(product)
    return product


# ─── Основной процесс парсинга ────────────────────────────────────────────────

async def scrape_all() -> list[Product]:
    """
    Полный цикл парсинга:
    1. Обходит все категории из START_URLS.
    2. Собирает ссылки на карточки.
    3. Загружает каждую карточку и извлекает данные.
    """
    all_products: dict[str, Product] = {}

    async with httpx.AsyncClient() as client:
        for cat_url in START_URLS:
            cat_name = cat_url.rstrip("/").split("/")[-1]
            log.info("Парсинг категории: %s", cat_name)

            try:
                cat_html = await _fetch(client, cat_url)
            except Exception as exc:
                log.error("Ошибка загрузки категории %s: %s", cat_url, exc)
                continue

            card_infos = _parse_category_page(cat_html, cat_name)
            log.info("  Найдено карточек: %d", len(card_infos))

            for info in card_infos:
                url = info["url"]
                if url in all_products:
                    continue

                await asyncio.sleep(SCRAPER_REQUEST_DELAY)

                try:
                    prod_html = await _fetch(client, url)
                    product = _parse_product_page(prod_html, info)
                    all_products[product.url] = product
                    log.debug("  ✓ %s (арт. %s)", product.name, product.article)
                except Exception as exc:
                    log.error("  Ошибка парсинга %s: %s", url, exc)

    result = list(all_products.values())
    log.info("Всего собрано товаров: %d", len(result))
    return result


# ─── Delta Update ──────────────────────────────────────────────────────────────

def _load_existing() -> dict[str, Product]:
    """Загружает ранее сохранённые товары из JSON."""
    if not RAW_PRODUCTS_PATH.exists():
        return {}
    try:
        data = json.loads(RAW_PRODUCTS_PATH.read_text(encoding="utf-8"))
        return {p["url"]: Product(**p) for p in data}
    except Exception as exc:
        log.warning("Не удалось загрузить %s: %s", RAW_PRODUCTS_PATH, exc)
        return {}


def _save_products(products: dict[str, Product]) -> None:
    """Сохраняет товары в JSON."""
    data = [p.model_dump() for p in products.values()]
    RAW_PRODUCTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Сохранено %d товаров в %s", len(data), RAW_PRODUCTS_PATH)


async def run_delta_update() -> dict[str, list[str]]:
    """
    Запускает парсинг и применяет Delta Update.

    Возвращает отчёт: {"added": [...], "updated": [...], "removed": [...], "unchanged": [...]}.
    """
    existing = _load_existing()
    fresh = await scrape_all()

    fresh_by_url = {p.url: p for p in fresh}

    report: dict[str, list[str]] = {
        "added": [],
        "updated": [],
        "removed": [],
        "unchanged": [],
    }

    # Новые и изменённые
    for url, product in fresh_by_url.items():
        if url not in existing:
            report["added"].append(product.article)
        elif existing[url].content_hash != product.content_hash:
            report["updated"].append(product.article)
        else:
            report["unchanged"].append(product.article)

    # Удалённые (исчезли с сайта)
    if SCRAPER_REMOVE_MISSING:
        for url in existing:
            if url not in fresh_by_url:
                report["removed"].append(existing[url].article)

    # Сохраняем актуальный набор
    _save_products(fresh_by_url)

    log.info(
        "Delta Update: добавлено=%d, обновлено=%d, удалено=%d, без изменений=%d",
        len(report["added"]),
        len(report["updated"]),
        len(report["removed"]),
        len(report["unchanged"]),
    )
    return report


# ─── Чанкирование для векторной БД ────────────────────────────────────────────

def process_to_chunks(products: list[Product] | None = None) -> list[dict]:
    """
    Превращает список товаров в текстовые чанки для ChromaDB.

    Каждый чанк содержит:
    - id: уникальный идентификатор (article)
    - text: человекочитаемый текст для эмбеддинга
    - metadata: структурированные поля для фильтрации
    """
    if products is None:
        existing = _load_existing()
        products = list(existing.values())

    chunks: list[dict] = []

    for p in products:
        chars_text = "; ".join(f"{k}: {v}" for k, v in p.characteristics.items())

        text_parts = [
            f"Название: {p.name}",
            f"Артикул: {p.article}",
        ]
        if p.category:
            text_parts.append(f"Категория: {p.category}")
        if p.price:
            text_parts.append(f"Цена: {p.price}")
        if chars_text:
            text_parts.append(f"Характеристики: {chars_text}")
        if p.description:
            text_parts.append(f"Описание: {p.description[:1500]}")
        text_parts.append(f"Ссылка: {p.url}")

        chunks.append({
            "id": p.article,
            "text": "\n".join(text_parts),
            "metadata": {
                "article": p.article,
                "name": p.name,
                "category": p.category or "",
                "price": p.price or "",
                "url": p.url,
                "material": p.characteristics.get("Материал", ""),
                "location": p.characteristics.get("Место применения", ""),
                "tags": ", ".join(p.tags),
            },
        })

    log.info("Создано %d чанков для векторной БД", len(chunks))
    return chunks
