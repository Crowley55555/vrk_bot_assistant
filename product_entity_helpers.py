"""
Эвристики для запросов о конкретной модели / серии / артикуле.

Семантический поиск в Chroma возвращает «похожие» документы; для запросов вида
«расскажи про диффузор Konika» нужно не показывать карточки, если в name/article
нет совпадения с сущностью из запроса.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Слова категории / общие — не считаются целевой сущностью
_CATEGORY_STOP: frozenset[str] = frozenset({
    "диффузор", "диффузоры", "решетка", "решетки", "решётка", "решётки",
    "вентиляционная", "вентиляционные", "вентиляционный", "щелевые", "щелевая",
    "корзина", "корзины", "воздухораспределитель", "воздухораспределители",
    "детали", "адаптер", "адаптеры", "клапан", "клапаны", "шумоглушитель",
    "товар", "товары", "модель", "модели", "серия", "серии", "артикул",
    "нужен", "нужна", "нужно", "покажи", "покажите", "есть", "ли",
    "что", "такое", "какой", "какая", "какие", "про", "для", "наш", "наша",
})

# Паттерны кода серии / артикула (латиница, дефисы)
_CODE_RE = re.compile(
    r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,15}(?:[-./][A-ZА-ЯЁ0-9]{1,12})*\b",
    re.IGNORECASE,
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
    """
    Извлекает кандидатов сущности (модель, серия, имя) из текста.
    Порядок сохраняется, дубликаты убираются.
    """
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

    for word in re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-z0-9А-Яа-яЁё./-]{2,}", t):
        nt = normalize_text(word)
        if nt in _CATEGORY_STOP or len(nt) < 3:
            continue
        if nt not in seen:
            seen[nt] = None
            out.append(word.strip())

    return out


def is_specific_product_query(message: str, entities: list[str] | None = None) -> bool:
    """
    True, если пользователь, вероятно, спрашивает о конкретной позиции, а не о категории.
    """
    if entities is None:
        entities = extract_product_entities(message)
    if not entities:
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
    """Проверяет вхождение сущности (нормализованно) в поля метаданных."""
    blob = _metadata_blob(meta)
    ent = normalize_text(entity)
    if len(ent) < 2:
        return False
    if ent in blob:
        return True
    ent_compact = ent.replace("-", "").replace(".", "").replace("/", "")
    if len(ent_compact) >= 3 and ent_compact in blob.replace("-", "").replace(".", "").replace("/", ""):
        return True
    return False


def filter_results_by_entities(
    results: list[dict],
    entities: list[str],
) -> list[dict]:
    """Оставляет только документы, где метаданные содержат хотя бы одну сущность."""
    if not entities:
        return []
    out: list[dict] = []
    for r in results:
        meta = r.get("metadata") or {}
        if any(product_matches_entity(meta, e) for e in entities):
            out.append(r)
    return out
