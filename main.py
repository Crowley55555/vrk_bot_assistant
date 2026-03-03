"""
FastAPI-бэкенд бота-консультанта ООО "Завод ВРК".

Реализует:
- Динамическую воронку (Dynamic Funnel) со Smart Routing для решеток.
- RAG-поиск с Metadata Filtering + sub_category и валидацией.
- Умный анализ свободного текста.
- Единый эндпоинт /api/chat для веб-виджета и Telegram.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from config import (
    FUNNEL_SCENARIOS,
    GRILLE_FEATURE_LABELS,
    GRILLE_MOUNT_OPTIONS,
    MANAGER_CONTACTS,
    PRODUCT_TYPE_STEP,
    STATIC_DIR,
    SUBCATEGORY_RULES,
    SYSTEM_PROMPT,
)
from llm_factory import get_llm
from logger import get_logger
from models import ButtonOption, ChatAction, ChatRequest, ChatResponse
from scheduler import start_scheduler
from vector_store import get_collection, reindex_all, search

log = get_logger(__name__)

# ─── Хранилище сессий ─────────────────────────────────────────────────────────

_sessions: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "funnel_phase": None,       # None | "product_type" | "scenario"
    "scenario_key": None,
    "step_idx": 0,
    "active_filters": {},
    "history": [],
    # Smart Routing (grille)
    "grille_phase": None,       # None | "mount" | "feature" | "done"
    "allowed_subcats": [],      # допустимые slug подкатегорий
    "grille_routing": [],       # стек решений [{step, value, subcats_before}]
})


def _get_session(session_id: str) -> dict[str, Any]:
    return _sessions[session_id]


def _reset_funnel(session_id: str) -> None:
    s = _get_session(session_id)
    s["funnel_phase"] = None
    s["scenario_key"] = None
    s["step_idx"] = 0
    s["active_filters"] = {}
    s["grille_phase"] = None
    s["allowed_subcats"] = []
    s["grille_routing"] = []


def _get_scenario(session_id: str) -> dict:
    key = _get_session(session_id).get("scenario_key") or "_default"
    return FUNNEL_SCENARIOS.get(key, FUNNEL_SCENARIOS["_default"])


# ─── Навигация ────────────────────────────────────────────────────────────────

def _make_buttons(step_config: dict) -> list[ButtonOption]:
    return [
        ButtonOption(
            label=opt["label"],
            value=opt.get("filter_value") or opt.get("value") or opt["label"],
        )
        for opt in step_config.get("options", [])
    ]


def _goto_main_menu(session_id: str) -> ChatResponse:
    _reset_funnel(session_id)
    _get_session(session_id)["funnel_phase"] = "product_type"
    return ChatResponse(
        reply=PRODUCT_TYPE_STEP["question"],
        action=ChatAction.ASK_QUESTION,
        buttons=_make_buttons(PRODUCT_TYPE_STEP),
    )


def _current_step_response(session_id: str) -> ChatResponse:
    session = _get_session(session_id)
    step = _get_scenario(session_id)["steps"][session["step_idx"]]
    return ChatResponse(
        reply=step["question"],
        action=ChatAction.ASK_QUESTION,
        buttons=_make_buttons(step),
    )


def _activate_scenario(session_id: str, scenario_key: str) -> ChatResponse:
    session = _get_session(session_id)
    effective_key = scenario_key if scenario_key in FUNNEL_SCENARIOS else "_default"
    scenario = FUNNEL_SCENARIOS[effective_key]

    session["scenario_key"] = effective_key
    session["funnel_phase"] = "scenario"
    session["step_idx"] = 0
    session["grille_phase"] = None
    session["grille_routing"] = []

    if scenario_key:
        session["active_filters"]["product_type"] = scenario_key

    for k, v in scenario.get("auto_filters", {}).items():
        session["active_filters"][k] = v

    if effective_key == "grille":
        session["allowed_subcats"] = list(SUBCATEGORY_RULES.keys())

    if not scenario["steps"]:
        return _do_filtered_search_sync(session_id, "")

    return _current_step_response(session_id)


# ═══════════════════════════════════════════════════════════════════════════════
# SMART ROUTING (подкатегории решеток)
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_subcats_by_location(location: str) -> list[str]:
    if not location:
        return list(SUBCATEGORY_RULES.keys())
    return [
        slug for slug, rules in SUBCATEGORY_RULES.items()
        if location in rules.get("location", [])
    ]


def _filter_subcats_by_mount(
    subcats: list[str], location: str, mount_value: str,
) -> list[str]:
    if not mount_value:
        return subcats
    chosen_mounts: set[str] = set()
    for opt in GRILLE_MOUNT_OPTIONS.get(location, []):
        if opt["value"] == mount_value:
            chosen_mounts = set(opt["mounts"])
            break
    if not chosen_mounts:
        for loc_opts in GRILLE_MOUNT_OPTIONS.values():
            for opt in loc_opts:
                if opt["value"] == mount_value:
                    chosen_mounts = set(opt["mounts"])
                    break
            if chosen_mounts:
                break
    if not chosen_mounts:
        return subcats
    result = [
        slug for slug in subcats
        if set(SUBCATEGORY_RULES.get(slug, {}).get("mount", [])) & chosen_mounts
    ]
    return result or subcats


def _filter_subcats_by_feature(subcats: list[str], feature: str) -> list[str]:
    if not feature:
        return subcats
    result = [
        slug for slug in subcats
        if SUBCATEGORY_RULES.get(slug, {}).get("feature") == feature
    ]
    return result or subcats


def _grille_mount_options(location: str, subcats: list[str]) -> list[dict]:
    """Возвращает варианты монтажа, для которых есть подкатегории."""
    cfg = GRILLE_MOUNT_OPTIONS.get(location, GRILLE_MOUNT_OPTIONS.get("indoor", []))
    available = []
    for opt in cfg:
        mounts_set = set(opt["mounts"])
        if any(mounts_set & set(SUBCATEGORY_RULES.get(s, {}).get("mount", [])) for s in subcats):
            available.append(opt)
    return available


def _grille_feature_options(subcats: list[str]) -> list[dict]:
    """Возвращает уникальные feature-варианты из оставшихся подкатегорий."""
    seen: set[str] = set()
    options: list[dict] = []
    for slug in subcats:
        feat = SUBCATEGORY_RULES.get(slug, {}).get("feature", "general")
        if feat not in seen:
            seen.add(feat)
            options.append({
                "label": GRILLE_FEATURE_LABELS.get(feat, feat),
                "value": feat,
            })
    return options


def _grille_advance(session_id: str, prefix: str = "") -> ChatResponse:
    """
    Определяет следующий динамический шаг Smart Routing.

    Если осталась 1 подкатегория или все уточнения пройдены —
    переходит к статическим шагам (size_group).
    """
    session = _get_session(session_id)
    subcats = session["allowed_subcats"]
    location = session["active_filters"].get("location", "")
    phase = session["grille_phase"]

    # ── Шаг: Монтаж (если ещё не пройден) ──
    if phase is None:
        mount_opts = _grille_mount_options(location, subcats)
        if len(mount_opts) > 1:
            session["grille_phase"] = "mount"
            buttons = [ButtonOption(label=o["label"], value=o["value"]) for o in mount_opts]
            buttons.append(ButtonOption(label="Не важно", value="any"))
            return ChatResponse(
                reply=prefix + "Как будет выполнен монтаж решетки?",
                action=ChatAction.ASK_QUESTION,
                buttons=buttons,
            )
        elif len(mount_opts) == 1:
            subcats = _filter_subcats_by_mount(subcats, location, mount_opts[0]["value"])
            session["allowed_subcats"] = subcats

    # ── Шаг: Особенности (если >1 feature осталось) ──
    if phase in (None, "mount_done"):
        feat_opts = _grille_feature_options(subcats)
        if len(feat_opts) > 1:
            session["grille_phase"] = "feature"
            buttons = [ButtonOption(label=o["label"], value=o["value"]) for o in feat_opts]
            buttons.append(ButtonOption(label="Не важно", value="any"))
            return ChatResponse(
                reply=prefix + "Какие требования к решетке?",
                action=ChatAction.ASK_QUESTION,
                buttons=buttons,
            )

    # ── Routing завершён → к size_group ──
    session["grille_phase"] = "done"
    session["step_idx"] = 1  # size_group — steps[1] в grille

    if len(subcats) == 1:
        label = SUBCATEGORY_RULES.get(subcats[0], {}).get("label", "")
        if label:
            prefix += f"Подобрали: {label}.\n\n"
    elif subcats:
        count = len(subcats)
        prefix += f"Подходящих типов: {count}.\n\n"

    step = _get_scenario(session_id)["steps"][1]
    return ChatResponse(
        reply=prefix + step["question"],
        action=ChatAction.ASK_QUESTION,
        buttons=_make_buttons(step),
    )


def _grille_handle_answer(session_id: str, message: str) -> ChatResponse:
    """Обрабатывает ответ на динамический шаг Smart Routing."""
    session = _get_session(session_id)
    phase = session["grille_phase"]
    location = session["active_filters"].get("location", "")

    session["grille_routing"].append({
        "step": phase,
        "value": message,
        "subcats_before": list(session["allowed_subcats"]),
    })

    if phase == "mount":
        subcats = _filter_subcats_by_mount(session["allowed_subcats"], location, message)
        session["allowed_subcats"] = subcats
        session["grille_phase"] = "mount_done"
        return _grille_advance(session_id)

    if phase == "feature":
        subcats = _filter_subcats_by_feature(session["allowed_subcats"], message)
        session["allowed_subcats"] = subcats
        session["grille_phase"] = "feature_done"
        return _grille_advance(session_id)

    return _grille_advance(session_id)


def _grille_back(session_id: str) -> ChatResponse:
    """Навигация «Назад» внутри Smart Routing."""
    session = _get_session(session_id)
    routing = session["grille_routing"]

    if routing:
        last = routing.pop()
        session["allowed_subcats"] = last["subcats_before"]
        prev_step = last["step"]

        if prev_step == "mount":
            session["grille_phase"] = None
            session["active_filters"].pop("location", None)
            session["step_idx"] = 0
            return _current_step_response(session_id)

        if prev_step == "feature":
            session["grille_phase"] = "mount_done"
            if routing and routing[-1]["step"] == "mount":
                session["grille_phase"] = "mount"
                return _grille_advance(session_id)
            session["grille_phase"] = None
            return _grille_advance(session_id)

    session["grille_phase"] = None
    session["active_filters"].pop("location", None)
    session["allowed_subcats"] = list(SUBCATEGORY_RULES.keys())
    session["step_idx"] = 0
    return _current_step_response(session_id)


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Запуск FastAPI-бэкенда …")
    try:
        get_llm()
    except RuntimeError as exc:
        log.critical(str(exc))

    col = get_collection()
    if col.count() == 0:
        log.info("ChromaDB пуста — попытка индексации из raw_products.json …")
        reindex_all()

    sched = start_scheduler()
    yield
    sched.shutdown(wait=False)
    log.info("FastAPI-бэкенд остановлен.")


app = FastAPI(title="Бот-консультант ВРК", version="3.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ RAG / LLM
# ═══════════════════════════════════════════════════════════════════════════════

def _build_context(results: list[dict]) -> str:
    if not results:
        return "В базе знаний ничего не найдено по данному запросу."
    parts = []
    for i, r in enumerate(results, 1):
        meta = r.get("metadata", {})
        raw_json = meta.get("raw_attrs_json", "{}")
        try:
            raw_attrs = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            raw_attrs = {}
        attrs_str = ", ".join(f"{k}: {v}" for k, v in raw_attrs.items()) if raw_attrs else "нет данных"
        parts.append(
            f"--- Товар {i} ---\n{r['text']}\n"
            f"Фильтры: location={meta.get('location','?')}, "
            f"product_type={meta.get('product_type','?')}, "
            f"size_group={meta.get('size_group','?')}\n"
            f"Характеристики: {attrs_str}"
        )
    return "\n\n".join(parts)


def _format_active_filters(session_id: str) -> str:
    session = _get_session(session_id)
    active = session.get("active_filters", {})
    if not active:
        return "Не заданы (свободный режим)"

    scenario = _get_scenario(session_id)
    all_steps = [PRODUCT_TYPE_STEP] + scenario.get("steps", [])
    step_map = {s["step_id"]: s for s in all_steps}

    parts = []
    for step_id, value in active.items():
        if not value:
            parts.append(f"{step_id}: не важно")
            continue
        step_cfg = step_map.get(step_id, {})
        label = value
        for opt in step_cfg.get("options", []):
            if opt.get("filter_value") == value:
                label = f"{opt['label']} ({value})"
                break
        parts.append(f"{step_id}: {label}")

    subcats = session.get("allowed_subcats", [])
    if subcats and len(subcats) <= 5:
        labels = [SUBCATEGORY_RULES.get(s, {}).get("label", s) for s in subcats]
        parts.append(f"подкатегории: {', '.join(labels)}")

    return ", ".join(parts)


async def _ask_llm(user_message: str, session_id: str, context: str) -> str:
    llm = get_llm()
    session = _get_session(session_id)
    filters_text = _format_active_filters(session_id)
    system_msg = SystemMessage(
        content=SYSTEM_PROMPT.format(context=context, active_filters=filters_text)
    )
    history = session["history"][-20:]
    messages = [system_msg] + history + [HumanMessage(content=user_message)]
    try:
        response: AIMessage = await llm.ainvoke(messages)
        answer = response.content
    except Exception as exc:
        log.error("Ошибка LLM: %s", exc)
        answer = "Извините, произошла техническая ошибка. Попробуйте ещё раз или свяжитесь с менеджером."
    session["history"].append(HumanMessage(content=user_message))
    session["history"].append(AIMessage(content=answer))
    return answer


# ═══════════════════════════════════════════════════════════════════════════════
# ПОИСК С ФИЛЬТРАЦИЕЙ, ВАЛИДАЦИЕЙ И SUB_CATEGORY
# ═══════════════════════════════════════════════════════════════════════════════

def _build_where_filter(
    active_filters: dict[str, str],
    allowed_subcats: list[str] | None = None,
) -> dict | None:
    conditions = [{k: {"$eq": v}} for k, v in active_filters.items() if v]
    if allowed_subcats:
        conditions.append({"category": {"$in": allowed_subcats}})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _validate_product(meta: dict, active_filters: dict[str, str]) -> bool:
    for key, value in active_filters.items():
        if not value:
            continue
        product_value = meta.get(key, "")
        if product_value and product_value != value:
            return False
    return True


def _search_with_fallback(
    query: str,
    active_filters: dict[str, str],
    scenario: dict,
    allowed_subcats: list[str] | None = None,
    n_results: int = 8,
) -> list[dict]:
    where = _build_where_filter(active_filters, allowed_subcats)
    results = search(query, n_results=n_results, where=where)
    validated = [r for r in results if _validate_product(r.get("metadata", {}), active_filters)]
    if validated:
        return validated

    relaxable = {k: v for k, v in active_filters.items() if v}
    step_ids = list(reversed([s["step_id"] for s in scenario.get("steps", [])]))

    for key_to_relax in step_ids:
        if key_to_relax in relaxable:
            relaxable.pop(key_to_relax)
            relaxed = _build_where_filter(relaxable, allowed_subcats)
            results = search(query, n_results=n_results, where=relaxed)
            validated = [r for r in results if _validate_product(r.get("metadata", {}), relaxable)]
            if validated:
                log.info("Fallback: убран фильтр '%s', найдено %d", key_to_relax, len(validated))
                return validated

    if allowed_subcats:
        relaxed = _build_where_filter(relaxable)
        results = search(query, n_results=n_results, where=relaxed)
        if results:
            log.info("Fallback: убраны subcategory фильтры, найдено %d", len(results))
            return results

    raw = search(query, n_results=n_results)
    pt = active_filters.get("product_type", "")
    return [r for r in raw if _validate_product(r.get("metadata", {}), {"product_type": pt})] or raw[:n_results]


def _build_search_query(session_id: str) -> str:
    session = _get_session(session_id)
    scenario = _get_scenario(session_id)
    all_steps = [PRODUCT_TYPE_STEP] + scenario.get("steps", [])
    step_map = {s["step_id"]: s for s in all_steps}
    parts = []
    for step_id, value in session["active_filters"].items():
        if not value:
            continue
        step_cfg = step_map.get(step_id, {})
        for opt in step_cfg.get("options", []):
            if opt.get("filter_value") == value:
                parts.append(opt["label"])
                break

    subcats = session.get("allowed_subcats", [])
    if subcats and len(subcats) <= 3:
        for s in subcats:
            label = SUBCATEGORY_RULES.get(s, {}).get("label", "")
            if label:
                parts.append(label)

    return " ".join(parts) if parts else "вентиляционное оборудование"


def _best_product_data(results: list[dict]) -> dict | None:
    if not results:
        return None
    best = results[0]["metadata"]
    return {
        "name": best.get("name", ""),
        "article": best.get("article", ""),
        "price": best.get("price", ""),
        "url": best.get("url", ""),
        "category": best.get("category", ""),
        "location": best.get("location", ""),
    }


async def _do_filtered_search(session_id: str, user_message: str) -> ChatResponse:
    session = _get_session(session_id)
    scenario = _get_scenario(session_id)
    query = user_message or _build_search_query(session_id)
    subcats = session.get("allowed_subcats") or None
    results = _search_with_fallback(query, session["active_filters"], scenario, subcats)

    log.info(
        "Поиск | scenario=%s | filters=%s | subcats=%s | results=%d",
        session.get("scenario_key", "?"),
        session["active_filters"],
        subcats[:3] if subcats else "all",
        len(results),
    )

    context = _build_context(results)
    llm_answer = await _ask_llm(
        f"Клиент ищет: {query}. Подбери подходящие товары из контекста.",
        session_id, context,
    )
    product_data = _best_product_data(results)
    _reset_funnel(session_id)
    return ChatResponse(
        reply=llm_answer,
        action=ChatAction.SHOW_PRODUCT if product_data else ChatAction.CONTACT_MANAGER,
        product_data=product_data,
    )


def _do_filtered_search_sync(session_id: str, user_message: str) -> ChatResponse:
    _reset_funnel(session_id)
    return ChatResponse(
        reply="По выбранной категории выполняется поиск. Уточните запрос текстом.",
        action=ChatAction.ASK_QUESTION,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# РАСПОЗНАВАНИЕ ТЕКСТА
# ═══════════════════════════════════════════════════════════════════════════════

_SIZE_RE = re.compile(r"(\d+)\s*[×хxXХ]\s*(\d+)")


def _extract_filters_from_text(text: str) -> dict[str, str]:
    lower = text.lower()
    filters: dict[str, str] = {}

    if any(w in lower for w in ("решетк", "решётк")):
        filters["product_type"] = "grille"
    elif "диффузор" in lower:
        filters["product_type"] = "diffuser"
    elif "клапан" in lower:
        filters["product_type"] = "valve"
    elif any(w in lower for w in ("воздухораспределител", "воздухораздат")):
        filters["product_type"] = "distributor"
    elif any(w in lower for w in ("электропривод", "привод")):
        filters["product_type"] = "actuator"
    elif any(w in lower for w in ("фильтр", "hepa")):
        filters["product_type"] = "filter"

    if any(w in lower for w in ("фасад", "улиц", "наружн", "уличн", "снаружи", "внешн")):
        filters["location"] = "outdoor"
    elif any(w in lower for w in (
        "помещен", "внутр", "квартир", "офис", "потолок", "потолоч",
        "стен", "комнат", "дом", "кухн", "ванн", "туалет",
        "в пол", "наполн", "межкомнат", "переточн",
    )):
        filters["location"] = "indoor"

    m = _SIZE_RE.search(text)
    if m:
        max_side = max(int(m.group(1)), int(m.group(2)))
        filters["size_group"] = "small" if max_side < 1000 else "large"
    elif any(w in lower for w in ("маленьк", "небольш", "компактн", "мини")):
        filters["size_group"] = "small"
    elif any(w in lower for w in ("больш", "крупн", "промышленн")):
        filters["size_group"] = "large"

    # Smart Routing hints (grille)
    if any(w in lower for w in ("скрыт", "невидим", "под шпакл", "гипсокартон", "натяжн")):
        filters["grille_mount"] = "concealed"
    elif any(w in lower for w in ("напольн", "в пол")):
        filters["grille_mount"] = "floor"
    elif any(w in lower for w in ("потолоч", "в потолок")):
        filters["grille_mount"] = "ceiling_open"
    elif any(w in lower for w in ("переточн", "переток", "в дверь", "перегород")):
        filters["grille_mount"] = "transfer"

    if any(w in lower for w in ("акустич", "шумо", "звукоизол")):
        filters["grille_feature"] = "acoustic"
    elif "сотов" in lower:
        filters["grille_feature"] = "honeycomb"
    elif "сетч" in lower:
        filters["grille_feature"] = "mesh"
    elif any(w in lower for w in ("перфорир", "перфорац")):
        filters["grille_feature"] = "perforated"
    elif "щелев" in lower:
        filters["grille_feature"] = "slot"
    elif any(w in lower for w in ("декоратив", "дизайн")):
        filters["grille_feature"] = "decorative"
    elif "люк" in lower:
        filters["grille_feature"] = "hatch"
    elif any(w in lower for w in ("инерцион", "обратн")):
        filters["grille_feature"] = "inertial"

    return filters


def _validate_extracted(
    extracted: dict[str, str], scenario: dict, text: str,
) -> tuple[dict[str, str], list[str]]:
    valid = dict(extracted)
    warnings: list[str] = []

    m = _SIZE_RE.search(text)
    if m:
        max_side = max(int(m.group(1)), int(m.group(2)))
        max_allowed = scenario.get("max_size_mm", 9999)
        if max_side > max_allowed:
            warnings.append(
                f"Максимальный размер для данной категории: {max_allowed} мм. "
                f"Указанный ({max_side} мм) превышает допустимый."
            )
            valid.pop("size_group", None)

    return valid, warnings


def _is_known_option(message: str) -> bool:
    for opt in PRODUCT_TYPE_STEP["options"]:
        if opt.get("filter_value") == message or opt["label"] == message:
            return True
    for scenario in FUNNEL_SCENARIOS.values():
        for step in scenario["steps"]:
            for opt in step.get("options", []):
                if opt.get("filter_value") == message or opt["label"] == message:
                    return True
    return False


def _describe_extracted(extracted: dict[str, str]) -> str:
    all_steps = [PRODUCT_TYPE_STEP]
    for sc in FUNNEL_SCENARIOS.values():
        all_steps.extend(sc["steps"])
    step_map: dict[str, dict] = {}
    for s in all_steps:
        if s["step_id"] not in step_map:
            step_map[s["step_id"]] = s

    parts: list[str] = []
    for step_id, value in extracted.items():
        if step_id.startswith("grille_"):
            if step_id == "grille_mount":
                for loc_opts in GRILLE_MOUNT_OPTIONS.values():
                    for o in loc_opts:
                        if o["value"] == value:
                            parts.append(o["label"])
                            break
            elif step_id == "grille_feature":
                parts.append(GRILLE_FEATURE_LABELS.get(value, value))
            continue
        cfg = step_map.get(step_id, {})
        for opt in cfg.get("options", []):
            if opt.get("filter_value") == value:
                parts.append(opt["label"])
                break
        else:
            parts.append(value)
    return ", ".join(parts)


def _is_start_funnel(message: str) -> bool:
    triggers = [
        "старт", "начать", "подобрать", "помоги выбрать",
        "нужна решетка", "нужен диффузор", "хочу купить",
        "подбор", "каталог", "что есть",
    ]
    return any(t in message.lower().strip() for t in triggers)


def _is_contact_request(message: str) -> bool:
    triggers = [
        "менеджер", "связаться", "позвонить", "телефон",
        "контакт", "оператор", "человек",
    ]
    return any(t in message.lower().strip() for t in triggers)


def _apply_grille_text_routing(session_id: str, extracted: dict[str, str]) -> None:
    """Применяет Smart Routing hints из текста к сессии grille."""
    session = _get_session(session_id)
    location = extracted.get("location") or session["active_filters"].get("location", "")
    subcats = _filter_subcats_by_location(location) if location else list(SUBCATEGORY_RULES.keys())

    mount_hint = extracted.get("grille_mount")
    if mount_hint:
        subcats = _filter_subcats_by_mount(subcats, location, mount_hint)

    feature_hint = extracted.get("grille_feature")
    if feature_hint:
        subcats = _filter_subcats_by_feature(subcats, feature_hint)

    session["allowed_subcats"] = subcats
    session["grille_phase"] = "done" if (mount_hint or feature_hint) else None


# ═══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ОБРАБОТЧИК
# ═══════════════════════════════════════════════════════════════════════════════

async def process_message(request: ChatRequest) -> ChatResponse:
    session_id = request.session_id
    message = request.message.strip()
    session = _get_session(session_id)

    # ── Навигация ──
    if message == "__main_menu__":
        return _goto_main_menu(session_id)

    if message == "__back__":
        phase = session["funnel_phase"]
        if phase == "scenario":
            grille_phase = session.get("grille_phase")
            scenario = _get_scenario(session_id)

            if scenario.get("dynamic") and grille_phase in ("mount", "feature"):
                return _grille_back(session_id)

            if scenario.get("dynamic") and grille_phase == "done":
                idx = session["step_idx"]
                if idx > 1:
                    prev_step = scenario["steps"][idx - 1]
                    session["active_filters"].pop(prev_step["step_id"], None)
                    session["step_idx"] = idx - 1
                    return _current_step_response(session_id)
                else:
                    return _grille_back(session_id)

            idx = session["step_idx"]
            if idx > 0:
                prev_step = scenario["steps"][idx - 1]
                session["active_filters"].pop(prev_step["step_id"], None)
                session["step_idx"] = idx - 1
                return _current_step_response(session_id)
            else:
                session["active_filters"].pop("product_type", None)
                session["active_filters"].clear()
                session["funnel_phase"] = "product_type"
                session["scenario_key"] = None
                return ChatResponse(
                    reply=PRODUCT_TYPE_STEP["question"],
                    action=ChatAction.ASK_QUESTION,
                    buttons=_make_buttons(PRODUCT_TYPE_STEP),
                )
        return _goto_main_menu(session_id)

    # ── Связь с менеджером ──
    if _is_contact_request(message):
        _reset_funnel(session_id)
        return ChatResponse(
            reply=(
                f"Свяжитесь с нашим менеджером:\n"
                f"📞 {MANAGER_CONTACTS['phone']}\n"
                f"📧 {MANAGER_CONTACTS['email']}\n"
                f"📍 {MANAGER_CONTACTS['address']}\n"
                f"🕐 {MANAGER_CONTACTS['work_hours']}"
            ),
            action=ChatAction.CONTACT_MANAGER,
        )

    # ── Фаза: выбор категории ──
    if session["funnel_phase"] == "product_type":
        for opt in PRODUCT_TYPE_STEP["options"]:
            if opt["filter_value"] == message or opt["label"] == message:
                return _activate_scenario(session_id, opt["filter_value"] or "_default")

    # ── Фаза: шаги сценария ──
    if session["funnel_phase"] == "scenario":
        scenario = _get_scenario(session_id)

        # Grille Smart Routing: динамические шаги
        if scenario.get("dynamic") and session.get("grille_phase") in ("mount", "feature"):
            return _grille_handle_answer(session_id, message)

        idx = session["step_idx"]
        steps = scenario["steps"]

        if idx < len(steps):
            current_step = steps[idx]
            chosen_value = None
            for opt in current_step.get("options", []):
                if opt.get("filter_value") == message or opt["label"] == message:
                    chosen_value = opt.get("filter_value", "")
                    break

            if chosen_value is not None:
                session["active_filters"][current_step["step_id"]] = chosen_value

                # Grille: после location → Smart Routing
                if scenario.get("dynamic") and current_step["step_id"] == "location":
                    subcats = _filter_subcats_by_location(chosen_value)
                    session["allowed_subcats"] = subcats

                    if len(subcats) == 0:
                        session["grille_phase"] = "done"
                        session["step_idx"] = 1
                        return _current_step_response(session_id)

                    return _grille_advance(session_id)

                session["step_idx"] = idx + 1
                if session["step_idx"] < len(steps):
                    return _current_step_response(session_id)
                return await _do_filtered_search(session_id, _build_search_query(session_id))

    # ── Умный анализ свободного текста ──
    extracted = _extract_filters_from_text(message)

    if extracted:
        pt = extracted.get("product_type") or session.get("scenario_key")
        scenario_key = pt or "_default"
        scenario = FUNNEL_SCENARIOS.get(scenario_key, FUNNEL_SCENARIOS["_default"])

        valid_filters, warnings = _validate_extracted(extracted, scenario, message)

        if "product_type" in valid_filters and valid_filters["product_type"]:
            session["scenario_key"] = valid_filters["product_type"]
            session["active_filters"]["product_type"] = valid_filters["product_type"]
            scenario = _get_scenario(session_id)
            for k, v in scenario.get("auto_filters", {}).items():
                if k not in valid_filters:
                    session["active_filters"][k] = v

        for key, value in valid_filters.items():
            if key != "product_type" and not key.startswith("grille_"):
                session["active_filters"][key] = value

        # Smart Routing для grille через текст
        if session.get("scenario_key") == "grille":
            if "grille_mount" in extracted or "grille_feature" in extracted:
                _apply_grille_text_routing(session_id, extracted)
            elif "location" in valid_filters:
                session["allowed_subcats"] = _filter_subcats_by_location(valid_filters["location"])

        scenario = _get_scenario(session_id)
        steps = scenario.get("steps", [])
        step_ids = [s["step_id"] for s in steps]

        next_idx = None
        for i, sid in enumerate(step_ids):
            if sid not in session["active_filters"]:
                next_idx = i
                break

        is_grille_routing_done = (
            session.get("scenario_key") == "grille"
            and session.get("grille_phase") == "done"
        )

        if next_idx is not None:
            if session.get("scenario_key") == "grille" and not is_grille_routing_done:
                if next_idx == 0:
                    pass
                elif next_idx >= 1 and session.get("grille_phase") != "done":
                    return _grille_advance(session_id)

            session["funnel_phase"] = "scenario"
            session["step_idx"] = next_idx
            step_cfg = steps[next_idx]

            warning_prefix = "\n".join(f"⚠️ {w}" for w in warnings)
            understood = _describe_extracted(valid_filters)
            confirm_prefix = f"✅ Понял: {understood}." if understood else ""
            prefix_parts = [p for p in (warning_prefix, confirm_prefix) if p]
            prefix = "\n\n".join(prefix_parts)
            if prefix:
                prefix += "\n\n"

            return ChatResponse(
                reply=prefix + step_cfg["question"],
                action=ChatAction.ASK_QUESTION,
                buttons=_make_buttons(step_cfg),
            )

        return await _do_filtered_search(session_id, message)

    # ── Триггеры начала воронки ──
    if _is_start_funnel(message) and session["funnel_phase"] is None:
        return _goto_main_menu(session_id)

    # ── Свободный вопрос (RAG) ──
    results = search(message, n_results=5)
    context = _build_context(results)
    llm_answer = await _ask_llm(message, session_id, context)

    if session["funnel_phase"] in ("product_type", "scenario"):
        if session["funnel_phase"] == "product_type":
            step_cfg = PRODUCT_TYPE_STEP
        else:
            scenario = _get_scenario(session_id)
            idx = session["step_idx"]
            step_cfg = scenario["steps"][idx] if idx < len(scenario["steps"]) else PRODUCT_TYPE_STEP
        return ChatResponse(
            reply=llm_answer + f"\n\n{step_cfg['question']}",
            action=ChatAction.ASK_QUESTION,
            buttons=_make_buttons(step_cfg),
        )

    product_data = None
    action = ChatAction.ASK_QUESTION
    if results and results[0]["distance"] < 0.7:
        product_data = _best_product_data(results)
        action = ChatAction.SHOW_PRODUCT

    return ChatResponse(reply=llm_answer, action=action, product_data=product_data)


# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest) -> ChatResponse:
    log.info("Запрос [%s] session=%s: %s", request.source, request.session_id[:8], request.message[:100])
    response = await process_message(request)
    log.info("Ответ [%s] action=%s: %s", request.source, response.action.value, response.reply[:100])
    return response


@app.get("/health")
async def health_check() -> dict:
    col = get_collection()
    llm_ok = True
    try:
        get_llm()
    except RuntimeError:
        llm_ok = False
    return {"status": "ok", "llm_available": llm_ok, "chroma_documents": col.count()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
