"""
Парсер каталога ООО "Завод ВРК".

- Характеристики берутся только из карточек товаров, только из класса items4_attrs (все пары).
- Со страницы товара берутся только: описание (div.block_tab.text1), ссылка и цена.
- Сценарий воронки синхронизирован с этими характеристиками (Материал, Место применения, Форма и т.д.).
- При сохранении в БД чанки сортируются по пути сценария и получают scenario_block,
  чтобы при прохождении сценария поиск шёл только в нужном блоке базы.
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
    CATEGORY_SLUG_MAP,
    MAIN_CATEGORIES,
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
    """MD5 от ключевых полей — для отслеживания изменений (в т.ч. смена категории)."""
    blob = "|".join([
        product.name,
        product.price or "",
        product.description or "",
        product.category or "",
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

# Порядок проверки важен: сначала уточнённые материалы, потом общий metal
_MATERIAL_STAINLESS_KW = ["нержавеющая сталь", "нержавейка", "нержав"]
_MATERIAL_GALVANIZED_KW = ["оцинковка", "оцинкованная сталь", "оцинкован"]
_MATERIAL_ALUMINUM_KW = ["алюминий", "алюминиев"]
_MATERIAL_METAL_OTHER_KW = ["сталь", "металл", "латунь", "медь"]
_MATERIAL_PLASTIC_KW = ["пластик", "пвх", "полипропилен", "abs", "полистирол"]
_MATERIAL_WOOD_KW = ["дерево", "деревянный", "мдф", "шпон"]


def _normalize_material(raw_value: str) -> str:
    """Маппинг сырого значения материала в код фильтра: aluminum, galvanized, stainless_steel, metal, plastic, wood, unknown."""
    if not raw_value or not raw_value.strip():
        return "unknown"
    lower = raw_value.lower()
    for kw in _MATERIAL_STAINLESS_KW:
        if kw in lower:
            return "stainless_steel"
    for kw in _MATERIAL_GALVANIZED_KW:
        if kw in lower:
            return "galvanized"
    for kw in _MATERIAL_ALUMINUM_KW:
        if kw in lower:
            return "aluminum"
    for kw in _MATERIAL_METAL_OTHER_KW:
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
# Число (диаметр в мм): из значения вида «315», «200 мм», «Ø 400»
_DIAMETER_NUM_RE = re.compile(r"\d{2,4}")


def _normalize_size_group(name: str, raw_attrs: dict[str, str]) -> str:
    """Определяет размерную группу (small / large) по названию или характеристикам."""
    search_text = name + " " + " ".join(raw_attrs.values())
    m = _SIZE_RE.search(search_text)
    if m:
        max_side = max(int(m.group(1)), int(m.group(2)))
        return "small" if max_side < 1000 else "large"
    return "unknown"


def _normalize_round_diameter_group(raw_attrs: dict[str, str], name: str) -> str:
    """
    Для круглых решёток: группа по диаметру (мм).
    under_315 — до 315 мм, under_500 — до 500 мм, over_500 — более 500 мм.
    Ищет диаметр в полях «Диаметр», «Размер», в названии (Ø 200, 315 мм и т.п.).
    """
    # Приоритет: явное поле Диаметр, затем Размер/Размеры, затем весь текст
    for key in ("Диаметр", "Размер", "Размеры"):
        val = raw_attrs.get(key, "").strip()
        if val:
            nums = [int(n) for n in _DIAMETER_NUM_RE.findall(val) if 50 <= int(n) <= 2000]
            if nums:
                d = max(nums)
                if d <= 315:
                    return "under_315"
                if d <= 500:
                    return "under_500"
                return "over_500"
    search_text = name + " " + " ".join(raw_attrs.values())
    for m in _DIAMETER_NUM_RE.finditer(search_text):
        d = int(m.group(0))
        if 50 <= d <= 2000:
            if d <= 315:
                return "under_315"
            if d <= 500:
                return "under_500"
            return "over_500"
    return "unknown"


def _normalize_regulated(raw_value: str) -> str | None:
    """Маппинг характеристики Регулируемая/Нерегулируемая в код фильтра (regulated / fixed)."""
    if not raw_value or not raw_value.strip():
        return None
    lower = raw_value.lower().strip()
    if "регулируем" in lower and "нерегулируем" not in lower:
        return "regulated"
    if "нерегулируем" in lower:
        return "fixed"
    return None


def _build_filters(raw_attrs: dict[str, str], name: str, category: str | None) -> dict[str, str]:
    """
    Собирает нормализованные фильтры из сырых характеристик товара.

    Обрабатывает ВСЕ найденные характеристики, маппит ключевые поля
    (Материал, Место применения, Регулируемая/Нерегулируемая) в строгие кодовые значения.
    """
    filters: dict[str, str] = {}

    mat_raw = raw_attrs.get("Материал", "")
    filters["material"] = _normalize_material(mat_raw) if mat_raw else "unknown"

    loc_raw = raw_attrs.get("Место применения", "") or raw_attrs.get("Исполнение", "")
    filters["location"] = _normalize_location(loc_raw) if loc_raw else "unknown"

    # Регулируемая/Нерегулируемая — по характеристике с сайта; если не указано — считаем нерегулируемой
    regulated_raw = (
        raw_attrs.get("Регулируемая/Нерегулируемая", "")
        or raw_attrs.get("Регулировка", "")
        or raw_attrs.get("Тип регулировки", "")
    )
    regulated = _normalize_regulated(regulated_raw)
    filters["regulated"] = regulated if regulated else "fixed"

    # Форма решётки (для фильтра в сценариях «На фасад», «В воздуховод»)
    form_raw = raw_attrs.get("Форма", "").strip().lower()
    all_text = (name + " " + " ".join(raw_attrs.values())).lower()
    if "прямоуголь" in form_raw:
        filters["form"] = "rectangular"
    elif "кругл" in form_raw:
        filters["form"] = "round"
    elif "квадрат" in form_raw:
        filters["form"] = "square"
    elif "цилиндр" in form_raw:
        filters["form"] = "cylindrical"
    elif form_raw:
        filters["form"] = form_raw[:50]  # иные значения как есть (нормализуем длину)
    elif "кругл" in all_text and _normalize_product_type(name, category) == "grille":
        # Круглые решётки: если в названии/атрибутах есть «кругл», а «Форма» на карточке пусто
        filters["form"] = "round"

    filters["product_type"] = _normalize_product_type(name, category)
    filters["size_group"] = _normalize_size_group(name, raw_attrs)
    # Круглые решётки: группа по диаметру (до 315 / до 500 / более 500 мм) для фильтра в сценарии
    if filters.get("form") == "round":
        filters["round_diameter_group"] = _normalize_round_diameter_group(raw_attrs, name)

    # Способ монтажа (встраиваемая / накладная) — для фильтра «На фасад» по типу решётки
    mount_raw = (raw_attrs.get("Способ монтажа", "") or "").strip().lower()
    if "встраива" in mount_raw:
        filters["installation"] = "embedded"
    elif "накладн" in mount_raw:
        filters["installation"] = "surface"

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
        card_attrs: dict[str, str] = {}
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
            # Характеристики только из блока items4_attrs на карточке каталога (все пары ключ-значение)
            items4_block = card.select_one("[class*='items4_attrs']") or card.select_one(".items4_attrs")
            if items4_block:
                card_attrs = _parse_items4_attrs(items4_block)

        items.append({
            "url": url,
            "name": name,
            "article": article,
            "price": price,
            "category": category_name,
            "tags": tags,
            "card_attrs": card_attrs,
        })

    return items


# ─── Парсинг блока характеристик ───────────────────────────────────────────────

def _parse_items4_attrs(block: BeautifulSoup) -> dict[str, str]:
    """
    Извлекает ВСЕ пары ключ-значение из блока items4_attrs (характеристики на карточке).
    На сайте ВРК ключ и значение в двух span, в двух div или в тексте «Ключ: Значение».
    Сценарий воронки синхронизирован с этими атрибутами (Материал, Место применения, Форма и т.д.).
    """
    attrs: dict[str, str] = {}
    if not block:
        return attrs
    # Строки: div/li/tr с двумя span или текст «Ключ: Значение»
    for row in block.select("div, li, tr"):
        spans = row.select("span")
        if len(spans) >= 2:
            k = _clean(spans[0].get_text()).rstrip(":—– \t")
            v = _clean(spans[1].get_text())
            if k and v:
                attrs[k] = v
        else:
            text = _clean(row.get_text())
            if ":" in text:
                parts = text.split(":", 1)
                k = parts[0].strip()
                v = parts[1].strip() if len(parts) > 1 else ""
                if k and v:
                    attrs[k] = v
    # Добираем по строкам текста блока («Ключ:» + следующая строка или «Ключ: Значение»)
    if not attrs:
        lines = block.get_text().replace("\r", "\n").split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.endswith(":") and len(line) < 80:
                k = line.rstrip(":—– \t").strip()
                v = _clean(lines[i + 1]) if i + 1 < len(lines) else ""
                if k and v and k not in ("Цена", "код", "Арт"):
                    attrs[k] = v
                i += 2
            elif ":" in line and len(line) < 150:
                parts = line.split(":", 1)
                k = parts[0].strip()
                v = parts[1].strip() if len(parts) > 1 else ""
                if k and v and k not in ("Цена", "код", "Арт"):
                    attrs[k] = v
                i += 1
            else:
                i += 1
    return attrs


# ─── Парсинг описания товара ────────────────────────────────────────────────────

def _parse_description(soup: BeautifulSoup) -> str | None:
    """
    Извлекает текстовое описание товара из вкладки «Описание».

    Источники (в порядке приоритета):
    1. div.tab_content.active.ck-content → div.block_tab.text1
    2. Первый div.block_tab.text1 на странице
    3. Fallback через <p> внутри product-description / product-text
    """
    # Способ 1: вкладка «Описание» (активная)
    tab = soup.select_one("div.tab_content.active.ck-content")
    if tab:
        block = tab.select_one("div.block_tab.text1")
        target = block if block else tab
        paragraphs = []
        for el in target.find_all(["p", "li"]):
            txt = _clean(el.get_text())
            if txt and len(txt) > 20:
                paragraphs.append(txt)
        if paragraphs:
            return "\n".join(paragraphs[:20])

    # Способ 2: первый block_tab.text1
    block = soup.select_one("div.block_tab.text1")
    if block:
        paragraphs = []
        for el in block.find_all(["p", "li"]):
            txt = _clean(el.get_text())
            if txt and len(txt) > 20:
                paragraphs.append(txt)
        if paragraphs:
            return "\n".join(paragraphs[:20])

    # Способ 3: fallback
    for cls in ("product-description", "product-text", "tab-content"):
        container = soup.select_one(f"div.{cls}")
        if container:
            paragraphs = []
            for p in container.find_all("p"):
                txt = _clean(p.get_text())
                if txt and len(txt) > 20:
                    paragraphs.append(txt)
            if paragraphs:
                return "\n".join(paragraphs[:15])

    return None


# ─── Парсинг страницы товара ───────────────────────────────────────────────────

def _parse_product_page(html: str, base_info: dict) -> Product:
    """
    Со страницы товара берём только: описание (div.block_tab.text1), ссылку и цену.
    Характеристики — только из карточек каталога (items4_attrs), со страницы не парсим.
    """
    soup = BeautifulSoup(html, "lxml")

    # Название (H1)
    h1 = soup.find("h1")
    name = _clean(h1.get_text()) if h1 else base_info.get("name", "")

    # Артикул (из карточки или со страницы)
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

    # Описание — только из div.block_tab.text1
    description = _parse_description(soup)

    # Характеристики только с карточки каталога (items4_attrs), со страницы не берём
    raw_attrs = base_info.get("card_attrs") or {}

    # Нормализованные фильтры под сценарий (из raw_attrs карточки)
    category = base_info.get("category", "")
    filters = _build_filters(raw_attrs, name, category)

    # Ссылка — из карточки
    url = base_info["url"]

    # Цена — с карточки или со страницы
    price = base_info.get("price")
    if not price:
        price_el = soup.find(string=re.compile(r"\d[\d\s]*[₽Р]"))
        if price_el:
            price = _clean(str(price_el))

    old_price: Optional[str] = None

    product = Product(
        article=article or hashlib.md5(url.encode()).hexdigest()[:8],
        name=name,
        url=url,
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
    1. Обходит все категории из START_URLS (данные парсятся и сохраняются по категориям).
    2. Собирает ссылки на карточки товаров.
    3. Загружает каждую карточку и извлекает данные + характеристики + фильтры.
    У каждого товара сохраняется поле category (slug страницы каталога).
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
                    # Товар уже добавлен с другой страницы каталога — обновляем категорию на текущую,
                    # чтобы раздел каталога (щелевые, диффузоры и т.д.) определялся последним вхождением
                    all_products[url].category = cat_name
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

def _scenario_block_from_filters(filters: dict[str, str], main_cat: str) -> str:
    """
    Строит идентификатор «блока» сценария для быстрого поиска по воронке.
    Совпадает с порядком шагов сценария: product_type → location → size_group [→ form для фасада].
    Решётки с формой «Цилиндрические» попадают в блок grille_duct_* (сценарий «В воздуховод»).
    """
    pt = filters.get("product_type") or main_cat or "other"
    loc = filters.get("location") or "unknown"
    sg = filters.get("size_group") or "unknown"
    form = (filters.get("form") or "").strip()
    if pt == "grille":
        if form == "cylindrical":
            loc = "duct"
        block = f"grille_{loc}_{sg}"
        if loc == "outdoor" and form:
            block += f"_{form}"
        return block
    if pt == "slot_grille":
        return f"slot_grille_{loc}_{sg}"
    return f"{pt}_{loc}_{sg}"


def process_to_chunks(products: list[Product] | None = None) -> list[dict]:
    """
    Превращает список товаров в текстовые чанки для ChromaDB.
    Чанки сортируются по пути сценария (product_type → location → size_group → form),
    чтобы при поиске по сценарию LLM шла в нужный блок базы.
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
            text_parts.append(f"Описание: {p.description[:3000]}")
        text_parts.append(f"Ссылка: {p.url}")

        sub_cat = p.category or ""
        main_cat = CATEGORY_SLUG_MAP.get(sub_cat, "")
        metadata: dict[str, str] = {
            "article": p.article,
            "name": p.name,
            "main_category": main_cat,
            "category": sub_cat,
            "price": p.price or "",
            "url": p.url,
            "tags": ", ".join(p.tags),
        }
        metadata.update(p.filters)
        if main_cat and main_cat in MAIN_CATEGORIES:
            metadata["product_type"] = main_cat
        metadata["scenario_block"] = _scenario_block_from_filters(metadata, main_cat)
        metadata["raw_attrs_json"] = json.dumps(p.raw_attrs, ensure_ascii=False)

        chunks.append({
            "id": p.article,
            "text": "\n".join(text_parts),
            "metadata": metadata,
        })

    # Сортировка как в сценарии: тип → место → размер → форма
    chunks.sort(
        key=lambda c: (
            c["metadata"].get("product_type", ""),
            c["metadata"].get("location", ""),
            c["metadata"].get("size_group", ""),
            c["metadata"].get("form", ""),
            c["metadata"].get("category", ""),
        )
    )

    log.info("Создано %d чанков для векторной БД (отсортировано по сценарию)", len(chunks))
    return chunks
