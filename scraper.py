"""
Умный парсер каталога ООО "Завод ВРК".

- Обходит страницы категорий, собирает ссылки на карточки товаров.
- Переходит на каждую карточку (Deep Crawl), извлекает полный набор данных,
  включая ВСЕ пары ключ-значение из блока характеристик (card1_attrs).
- Нормализует сырые характеристики в строгие фильтры (material, location,
  product_type, size_group) для Metadata Filtering в ChromaDB.
- Реализует инкрементальное обновление (Delta Update).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
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
        json.dumps(product.raw_attrs, sort_keys=True, ensure_ascii=False),
        json.dumps(product.filters, sort_keys=True, ensure_ascii=False),
    ])
    return hashlib.md5(blob.encode()).hexdigest()


def _abs_url(href: str) -> str:
    """Преобразует относительную ссылку в абсолютную."""
    if href.startswith("http"):
        return href
    return BASE_SITE_URL.rstrip("/") + "/" + href.lstrip("/")


# ═══════════════════════════════════════════════════════════════════════════════
# НОРМАЛИЗАЦИЯ АТРИБУТОВ → ФИЛЬТРЫ
# ═══════════════════════════════════════════════════════════════════════════════

_MATERIAL_METAL_KW = [
    "нержавеющая сталь", "нержавейка", "оцинковка", "оцинкованная сталь",
    "алюминий", "сталь", "металл", "латунь", "медь",
]
_MATERIAL_PLASTIC_KW = ["пластик", "пвх", "полипропилен", "abs", "полистирол"]
_MATERIAL_WOOD_KW = ["дерево", "деревянный", "мдф", "шпон"]


def _normalize_material(raw_value: str) -> str:
    """Маппинг сырого значения материала в код фильтра."""
    lower = raw_value.lower()
    for kw in _MATERIAL_METAL_KW:
        if kw in lower:
            return "metal"
    for kw in _MATERIAL_PLASTIC_KW:
        if kw in lower:
            return "plastic"
    for kw in _MATERIAL_WOOD_KW:
        if kw in lower:
            return "wood"
    return "unknown"


_LOCATION_OUTDOOR_KW = [
    "наружное", "наружный", "для фасада", "на фасад", "фасад",
    "уличный", "с улицы", "улица",
]
_LOCATION_INDOOR_KW = [
    "внутреннее", "внутренний", "для помещений", "помещение",
    "внутрь", "в стены", "в потолок", "в пол",
]


def _normalize_location(raw_value: str) -> str:
    """Маппинг места установки в код фильтра."""
    lower = raw_value.lower()
    for kw in _LOCATION_OUTDOOR_KW:
        if kw in lower:
            return "outdoor"
    for kw in _LOCATION_INDOOR_KW:
        if kw in lower:
            return "indoor"
    return "unknown"


def _normalize_product_type(name: str, category: str | None) -> str:
    """Определяет тип изделия по названию и категории."""
    combined = (name + " " + (category or "")).lower()
    if "диффузор" in combined:
        return "diffuser"
    if "клапан" in combined:
        return "valve"
    if any(w in combined for w in ("решетка", "решётка")):
        return "grille"
    if "воздухораспределител" in combined:
        return "distributor"
    if "электропривод" in combined:
        return "actuator"
    if any(w in combined for w in ("фильтр", "hepa")):
        return "filter"
    if "корзин" in combined:
        return "ac_basket"
    if "шумоглушител" in combined:
        return "silencer"
    return "other"


_SIZE_RE = re.compile(r"(\d+)\s*[×хxXХ]\s*(\d+)")


def _normalize_size_group(name: str, raw_attrs: dict[str, str]) -> str:
    """Определяет размерную группу (small / large) по названию или характеристикам."""
    search_text = name + " " + " ".join(raw_attrs.values())
    m = _SIZE_RE.search(search_text)
    if m:
        max_side = max(int(m.group(1)), int(m.group(2)))
        return "small" if max_side < 1000 else "large"
    return "unknown"


def _build_filters(raw_attrs: dict[str, str], name: str, category: str | None) -> dict[str, str]:
    """
    Собирает нормализованные фильтры из сырых характеристик товара.

    Обрабатывает ВСЕ найденные характеристики, маппит ключевые поля
    (Материал, Место применения) в строгие кодовые значения.
    """
    filters: dict[str, str] = {}

    mat_raw = raw_attrs.get("Материал", "")
    filters["material"] = _normalize_material(mat_raw) if mat_raw else "unknown"

    loc_raw = raw_attrs.get("Место применения", "") or raw_attrs.get("Исполнение", "")
    filters["location"] = _normalize_location(loc_raw) if loc_raw else "unknown"

    filters["product_type"] = _normalize_product_type(name, category)
    filters["size_group"] = _normalize_size_group(name, raw_attrs)

    return filters


# ═══════════════════════════════════════════════════════════════════════════════
# ПАРСИНГ HTML
# ═══════════════════════════════════════════════════════════════════════════════

# Ссылка на товар: /catalog/{slug_категории}/{slug_товара}
_PRODUCT_LINK_RE = re.compile(r"/catalog/[^/]+/[^/]+(?:\?|$|/)")


def _parse_category_page(html: str, category_name: str) -> list[dict]:
    """
    Из страницы категории собирает ссылки на карточки товаров (Deep Crawl).
    Каждая ссылка станет отправной точкой для парсинга страницы товара.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[dict] = []
    seen_urls: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = (a_tag.get("href") or "").strip()
        if not href or not _PRODUCT_LINK_RE.search(href):
            continue
        url = _abs_url(href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        path = href.split("?")[0].rstrip("/")
        if path.count("/") < 3:
            continue

        name = _clean(a_tag.get_text())
        if not name or len(name) < 2:
            name = _clean(a_tag.get("title")) or ""
        if not name:
            parent = a_tag.find_parent(["div", "article", "li"])
            if parent:
                name = _clean(parent.get_text())
        if not name or len(name) < 2:
            name = "Товар"

        article = ""
        price: Optional[str] = None
        tags: list[str] = []
        card = a_tag.find_parent(["div", "article", "li", "section"])
        if card:
            card_text = card.get_text()
            art_m = re.search(r"(?:Арт\.|код:)\s*(\d{3,})", card_text)
            if art_m:
                article = art_m.group(1)
            price_m = re.search(r"(\d[\d\s]*\s*[₽Р]/шт|\d[\d\s]*\s*₽)", card_text)
            if price_m:
                price = _clean(price_m.group(1))
            for label in ("Хит", "Акция", "Советуем", "Новинка"):
                if label in card_text:
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


# ─── Парсинг блока характеристик ───────────────────────────────────────────────

def _parse_card_attrs(soup: BeautifulSoup) -> dict[str, str]:
    """
    Извлекает ВСЕ пары ключ-значение из блока характеристик товара.

    Пробует три источника (в порядке приоритета):
    1. div.card1_attrs_wrap — sidebar карточки
    2. div.card_attrs — вкладка «Характеристики»
    3. Текстовый поиск заголовка «Характеристики» — универсальный fallback

    Количество пар может быть любым (2, 3, 5, …) — берём всё, что есть.
    """
    attrs: dict[str, str] = {}

    # Способ 1: sidebar (card1_attrs_wrap > card1_attr)
    wrap = soup.select_one("div.card1_attrs_wrap")
    if wrap:
        for row in wrap.select("div.card1_attr"):
            key_el = row.select_one("span.card1_attr_title")
            val_el = row.select_one("span.card1_attr_value")
            if key_el and val_el:
                k = _clean(key_el.get_text()).rstrip(":—– \t")
                v = _clean(val_el.get_text())
                if k and v:
                    attrs[k] = v
        if attrs:
            return attrs

    # Способ 2: вкладка (card_attrs > ul > li)
    card_attrs_div = soup.select_one("div.card_attrs")
    if card_attrs_div:
        for li in card_attrs_div.select("ul li"):
            key_el = li.select_one("div.attr_title")
            val_el = li.select_one("div.attr_value")
            if key_el and val_el:
                k = _clean(key_el.get_text()).rstrip(":—– \t")
                v = _clean(val_el.get_text())
                if k and v:
                    attrs[k] = v
        if attrs:
            return attrs

    # Способ 3: универсальный fallback по тексту «Характеристики»
    header = soup.find(string=re.compile(r"Характеристики", re.I))
    if header:
        parent = header.find_parent(["div", "section", "table"])
        if parent:
            for row in parent.find_all(["tr", "li", "div"]):
                cells = row.find_all(["td", "th", "span", "dt", "dd"])
                if len(cells) >= 2:
                    k = _clean(cells[0].get_text()).rstrip(":—– \t")
                    v = _clean(cells[1].get_text())
                    if k and v and k.lower() != "характеристики":
                        attrs[k] = v

    return attrs


# ─── Парсинг страницы товара ───────────────────────────────────────────────────

def _parse_product_page(html: str, base_info: dict) -> Product:
    """
    Deep Crawl: со страницы товара извлекает полные данные,
    включая все характеристики и нормализованные фильтры.
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
        m = re.search(r"-(\d+)$", base_info.get("url", ""))
        if m:
            article = m.group(1)

    # Описание
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

    # ── Характеристики (все пары ключ-значение) ──
    raw_attrs = _parse_card_attrs(soup)

    # ── Нормализованные фильтры ──
    category = base_info.get("category", "")
    filters = _build_filters(raw_attrs, name, category)

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
        category=category,
        raw_attrs=raw_attrs,
        filters=filters,
        tags=base_info.get("tags", []),
    )
    product.content_hash = _content_hash(product)
    return product


# ─── Основной процесс парсинга ────────────────────────────────────────────────

async def scrape_all() -> list[Product]:
    """
    Полный цикл Deep Crawl:
    1. Обходит все категории из START_URLS.
    2. Собирает ссылки на карточки товаров.
    3. Загружает каждую карточку и извлекает данные + характеристики + фильтры.
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

                await asyncio.sleep(random.uniform(0.5, SCRAPER_REQUEST_DELAY))

                try:
                    prod_html = await _fetch(client, url)
                    product = _parse_product_page(prod_html, info)
                    all_products[product.url] = product
                    log.debug(
                        "  ✓ %s (арт. %s) | фильтры: %s | attrs: %d шт.",
                        product.name,
                        product.article,
                        product.filters,
                        len(product.raw_attrs),
                    )
                except Exception as exc:
                    log.error("  Ошибка парсинга %s: %s", url, exc)

    result = list(all_products.values())
    log.info("Всего собрано товаров: %d", len(result))
    return result


# ─── Delta Update ──────────────────────────────────────────────────────────────

def _load_existing() -> dict[str, Product]:
    """Загружает ранее сохранённые товары из JSON (с миграцией старого формата)."""
    if not RAW_PRODUCTS_PATH.exists():
        return {}
    try:
        data = json.loads(RAW_PRODUCTS_PATH.read_text(encoding="utf-8"))
        products: dict[str, Product] = {}
        for p in data:
            if "characteristics" in p and "raw_attrs" not in p:
                p["raw_attrs"] = p.pop("characteristics")
            if "filters" not in p:
                p["filters"] = {}
            p.pop("characteristics", None)
            products[p["url"]] = Product(**p)
        return products
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

    for url, product in fresh_by_url.items():
        if url not in existing:
            report["added"].append(product.article)
        elif existing[url].content_hash != product.content_hash:
            report["updated"].append(product.article)
        else:
            report["unchanged"].append(product.article)

    if SCRAPER_REMOVE_MISSING:
        for url in existing:
            if url not in fresh_by_url:
                report["removed"].append(existing[url].article)

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
    - id: уникальный идентификатор (артикул)
    - text: человекочитаемый текст для эмбеддинга
    - metadata: нормализованные фильтры + служебные поля для where-clause

    ТЕСТ: после запуска убедитесь, что в metadata каждого чанка
    присутствуют ключи material, location, product_type, size_group.
    """
    if products is None:
        existing = _load_existing()
        products = list(existing.values())

    chunks: list[dict] = []
    seen_articles: set[str] = set()

    for p in products:
        if p.article in seen_articles:
            continue
        seen_articles.add(p.article)

        # Текст чанка для семантического поиска
        attrs_text = "; ".join(f"{k}: {v}" for k, v in p.raw_attrs.items())

        text_parts = [
            f"Название: {p.name}",
            f"Артикул: {p.article}",
        ]
        if p.category:
            text_parts.append(f"Категория: {p.category}")
        if p.price:
            text_parts.append(f"Цена: {p.price}")
        if attrs_text:
            text_parts.append(f"Характеристики: {attrs_text}")
        if p.description:
            text_parts.append(f"Описание: {p.description[:1500]}")
        text_parts.append(f"Ссылка: {p.url}")

        # Метаданные: нормализованные фильтры + служебные поля
        metadata: dict[str, str] = {
            "article": p.article,
            "name": p.name,
            "category": p.category or "",
            "price": p.price or "",
            "url": p.url,
            "tags": ", ".join(p.tags),
        }
        metadata.update(p.filters)
        metadata["raw_attrs_json"] = json.dumps(p.raw_attrs, ensure_ascii=False)

        chunks.append({
            "id": p.article,
            "text": "\n".join(text_parts),
            "metadata": metadata,
        })

    log.info("Создано %d чанков для векторной БД (дедуплицировано)", len(chunks))
    return chunks
