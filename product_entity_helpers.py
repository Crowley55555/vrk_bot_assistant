"""
Эвристики: точечный запрос о модели/серии vs общий каталог vs аналоги.

Семантический поиск Chroma даёт «похожие» документы — для Konika нельзя
показывать карточки без совпадения сущности в name/article/url/raw_attrs.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Слова категории / общие — не целевая сущность
_CATEGORY_STOP: frozenset[str] = frozenset({
    "диффузор", "диффузоры", "решетка", "решетки", "решётка", "решётки",
    "вентиляционная", "вентиляционные", "вентиляционный", "щелевые", "щелевая",
    "корзина", "корзины", "воздухораспределитель", "воздухораспределители",
    "детали", "адаптер", "адаптеры", "клапан", "клапаны", "шумоглушитель",
    "товар", "товары", "модель", "модели", "серия", "серии", "артикул",
    "нужен", "нужна", "нужно", "нужны", "покажи", "покажите", "есть", "ли",
    "что", "такое", "какой", "какая", "какие", "про", "для", "наш", "наша",
    "экран", "экраны", "панель", "панели",
})

# Коды серий латиницей (2–4 буквы + опц. суффикс)
_CODE_RE = re.compile(
    r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,15}(?:[-./][A-ZА-ЯЁ0-9]{1,12})*\b",
    re.IGNORECASE,
)
_SHORT_LATIN_CODE_RE = re.compile(
    r"\b[A-Z]{2,4}(?:-[A-Z0-9]{1,10})?\b",
)

_ANALOG_SUBSTR: tuple[str, ...] = (
    "аналог",
    "похож",
    "замен",
    "подбери замену",
    "подобрать замену",
    "что есть похож",
    "что похоже",
    "максимально близк",
)

_GENERIC_SUBSTR: tuple[str, ...] = (
    "покажи все",
    "покажите все",
    "какие есть решет",
    "какие есть щелев",
    "какие диффузор",
    "покажи диффузоры",
    "покажите диффузоры",
    "все диффузор",
    "ассортимент диффузор",
    "каталог диффузор",
    "нужны корзин",
    "корзины для кондиционеров",
    "какие корзин",
    "покажи воздухораспределител",
    "покажи клапан",
    "покажи шумоглушител",
    "покажи адаптер",
    "какие клапан",
    "список товар",
)


def normalize_text(s: str) -> str:
    t = (s or "").lower().replace("ё", "е")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _strip_query_prefixes(text: str) -> str:
    t = text.strip()
    low = t.lower()
    for prefix in (
        "расскажи про", "расскажите про", "что такое", "что за",
        "чем отличается", "опиши", "описание", "информация про",
        "характеристики", "расскажи", "расскажите",
    ):
        if low.startswith(prefix):
            t = t[len(prefix) :].strip(" :–-")
            low = t.lower()
            break
    return t


def extract_product_entities(message: str) -> list[str]:
    """Кандидаты сущности: серия, модель, артикул, имя."""
    t = _strip_query_prefixes(message)
    seen: dict[str, None] = {}
    out: list[str] = []

    for m in _CODE_RE.finditer(t):
        tok = m.group(0).strip()
        nt = normalize_text(tok)
        if len(nt) < 2 or nt in _CATEGORY_STOP:
            continue
        if nt not in seen:
            seen[nt] = None
            out.append(tok.strip())

    for m in _SHORT_LATIN_CODE_RE.finditer(t):
        tok = m.group(0).strip()
        if len(tok) < 2:
            continue
        nt = normalize_text(tok)
        if nt in _CATEGORY_STOP:
            continue
        if nt not in seen:
            seen[nt] = None
            out.append(tok.strip())

    for word in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-z0-9А-Яа-яЁё./-]{2,}", t):
        nt = normalize_text(word)
        if nt in _CATEGORY_STOP:
            continue
        if len(nt) < 3:
            continue
        if nt not in seen:
            seen[nt] = None
            out.append(word.strip())

    return out


def is_analog_or_similar_intent(message: str) -> bool:
    """Запрос на аналог / похожую позицию — допустима семантическая подборка карточек."""
    low = message.lower()
    return any(s in low for s in _ANALOG_SUBSTR)


def is_generic_catalog_query(message: str, entities: list[str] | None = None) -> bool:
    """
    Обзор категории / список без привязки к конкретной модели в запросе.
    """
    if entities is None:
        entities = extract_product_entities(message)
    low = message.lower()

    if any(s in low for s in _GENERIC_SUBSTR):
        return True

    if ("какие" in low or "покажи" in low or "нужны" in low) and not entities:
        if any(
            w in low
            for w in (
                "диффузор",
                "решет",
                "щелев",
                "корзин",
                "клапан",
                "воздухораспределител",
                "шумоглушител",
            )
        ):
            return True

    return False


def is_specific_product_query(
    message: str,
    entities: list[str] | None = None,
) -> bool:
    """
    Точечный запрос о модели/серии — нужна проверка совпадения в метаданных;
    не смешивать с общим каталогом и с запросом аналога.
    """
    if is_analog_or_similar_intent(message):
        return False
    if entities is None:
        entities = extract_product_entities(message)
    if not entities:
        return False
    if is_generic_catalog_query(message, entities):
        return False

    low = message.lower()
    if any(
        p in low
        for p in (
            "покажи диффузоры",
            "покажите диффузоры",
            "все диффузоры",
            "какие диффузоры",
            "ассортимент диффузор",
            "каталог диффузор",
        )
    ):
        return False

    good = [normalize_text(e) for e in entities if normalize_text(e) not in _CATEGORY_STOP]
    if not good:
        return False
    return True


def _metadata_blob(meta: dict[str, Any]) -> str:
    parts: list[str] = [
        str(meta.get("name", "") or ""),
        str(meta.get("article", "") or ""),
        str(meta.get("category", "") or ""),
        str(meta.get("url", "") or ""),
        str(meta.get("tags", "") or ""),
    ]
    raw = meta.get("raw_attrs_json") or "{}"
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(d, dict):
            parts.extend(str(v) for v in d.values())
    except (json.JSONDecodeError, TypeError):
        pass
    return normalize_text(" ".join(parts))


def product_matches_entity(meta: dict[str, Any], entity: str) -> bool:
    """Вхождение сущности в поля метаданных (нормализованно)."""
    blob = _metadata_blob(meta)
    ent = normalize_text(entity)
    if len(ent) < 2:
        return False
    if ent in blob:
        return True
    ent_compact = ent.replace("-", "").replace(".", "").replace("/", "")
    blob_compact = blob.replace("-", "").replace(".", "").replace("/", "")
    if len(ent_compact) >= 2 and ent_compact in blob_compact:
        return True
    return False


def filter_results_by_entities(
    results: list[dict],
    entities: list[str],
) -> list[dict]:
    """Документы, где метаданные содержат хотя бы одну сущность."""
    if not entities:
        return []
    out: list[dict] = []
    for r in results:
        meta = r.get("metadata") or {}
        if any(product_matches_entity(meta, e) for e in entities):
            out.append(r)
    return out


def filter_results_by_product_type(
    results: list[dict],
    product_type: str | None,
) -> list[dict]:
    """Оставить только товары с main_category / product_type в метаданных."""
    if not product_type:
        return results
    out: list[dict] = []
    for r in results:
        meta = r.get("metadata") or {}
        pt = (meta.get("product_type") or meta.get("main_category") or "").strip()
        if pt == product_type:
            out.append(r)
    return out


def rank_exact_or_near_exact_matches(
    results: list[dict],
    entities: list[str],
) -> list[dict]:
    """Сортирует: сначала совпадение в name/article, затем остальные."""
    if not results or not entities:
        return results

    def score(r: dict) -> tuple[int, float]:
        meta = r.get("metadata") or {}
        name = normalize_text(str(meta.get("name", "")))
        art = normalize_text(str(meta.get("article", "")))
        best = 0
        dist = r.get("distance", 1.0)
        for e in entities:
            ne = normalize_text(e)
            if ne in name or ne in art:
                best = max(best, 2)
            elif product_matches_entity(meta, e):
                best = max(best, 1)
        return (-best, dist)

    return sorted(results, key=score)
