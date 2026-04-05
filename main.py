"""
FastAPI-бэкенд бота-консультанта ООО "Завод ВРК".

Реализует:
- Динамическую воронку (Dynamic Funnel) со Smart Routing для решеток.
- RAG-поиск с Metadata Filtering + sub_category и валидацией.
- Умный анализ свободного текста.
- Единый эндпоинт /api/chat для веб-виджета и Telegram.
"""

from __future__ import annotations

import asyncio
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
    AC_BASKET_SUBCAT_FILTER,
    ACOUSTIC_STEPS,
    CATEGORY_SLUG_MAP,
    DISTRIBUTOR_SUBCAT_FILTER,
    VENT_PARTS_SUBCAT_FILTER,
    FACADE_SERIES,
    FACADE_STEPS,
    FUNNEL_SCENARIOS,
    GRILLE_FEATURE_LABELS,
    GRILLE_MOUNT_OPTIONS,
    INDOOR_FILLING_QUERY_HINTS,
    INDOOR_PRIORITY_QUERY_HINTS,
    INDOOR_PRIORITY_SUBCAT_HINTS,
    INDOOR_SERIES,
    INDOOR_STEPS,
    INDOOR_TYPE_QUERY_HINTS,
    INDOOR_TYPE_SUBCAT_HINTS,
    INTENT_TRIGGERS,
    MAIN_CATEGORIES,
    MANAGER_CONTACTS,
    PRODUCT_TYPE_STEP,
    SALES_ARGS,
    SLOT_GRILLE_SUBCAT_FILTER,
    SLOT_SERIES,
    SLOT_GKL_REQUIRED_KEYS,
    SLOT_STEPS,
    STATIC_DIR,
    SUBCATEGORY_RULES,
    SYSTEM_PROMPT,
)
from catalog_bootstrap import ensure_catalog_ready
from llm_factory import get_llm
from logger import get_logger
from models import ButtonOption, ChatAction, ChatRequest, ChatResponse
from product_entity_helpers import (
    extract_product_entities,
    filter_results_by_entities,
    filter_results_by_product_type,
    is_analog_or_similar_intent,
    is_generic_catalog_query,
    is_specific_product_query,
    rank_exact_or_near_exact_matches,
)
from scheduler import start_scheduler
from vector_store import get_collection, search, warmup_embedding_and_search

log = get_logger(__name__)

# ─── Хранилище сессий ─────────────────────────────────────────────────────────

_sessions: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "funnel_phase": None,       # None | "product_type" | "scenario" | "detail"
    "scenario_key": None,
    "step_idx": 0,
    "active_filters": {},
    "history": [],
    # Smart Routing (grille)
    "grille_phase": None,       # None | "mount" | "feature" | "done"
    "allowed_subcats": [],      # допустимые slug подкатегорий
    "grille_routing": [],       # стек решений [{step, value, subcats_before}]
    # Detail branch (CSV decision tree)
    "detail_branch": None,      # "facade" | "indoor" | "slot" | None
    "detail_step_idx": 0,
    "detail_answers": {},       # ответы на шаги детальной ветки
    "transfer_execution_hint": "",
    "detected_intents": {},     # analog, custom, mechanical_vent, budget, premium
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
    s["detail_branch"] = None
    s["detail_step_idx"] = 0
    s["detail_answers"] = {}
    s["transfer_execution_hint"] = ""
    s["detected_intents"] = {}


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
    scenario = _get_scenario(session_id)
    steps = scenario["steps"]
    idx = session["step_idx"]
    if idx >= len(steps):
        session["step_idx"] = max(0, len(steps) - 1)
        idx = session["step_idx"]
    if idx < 0:
        session["step_idx"] = 0
        idx = 0
    step = steps[idx]
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
    elif effective_key == "slot_grille":
        session["allowed_subcats"] = [
            slug for slug, cat in CATEGORY_SLUG_MAP.items() if cat == "slot_grille"
        ]
    else:
        # Диффузоры, корзины, воздухораспределители, детали — фильтр по подкатегориям не нужен
        session["allowed_subcats"] = []

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
            return ChatResponse(
                reply=prefix + "Какие требования к решетке?",
                action=ChatAction.ASK_QUESTION,
                buttons=buttons,
            )

    # ── Routing завершён → к статическим шагам ──
    session["grille_phase"] = "done"
    transfer_only = len(subcats) == 1 and subcats[0] == "reshetki-peretochnye"
    if transfer_only:
        # Для переточных решеток в каталоге используется алюминий — шаг material пропускаем.
        session["active_filters"]["material"] = "aluminum"
        session["step_idx"] = 2  # size_group
    else:
        session["step_idx"] = 1  # material

    if len(subcats) == 1:
        label = SUBCATEGORY_RULES.get(subcats[0], {}).get("label", "")
        if label:
            prefix += f"Подобрали: {label}.\n\n"
    elif subcats:
        count = len(subcats)
        prefix += f"Подходящих типов: {count}.\n\n"

    step = _get_scenario(session_id)["steps"][session["step_idx"]]
    return ChatResponse(
        reply=prefix + step["question"],
        action=ChatAction.ASK_QUESTION,
        buttons=_make_buttons(step),
    )


_GRILLE_INVALID_HINT = (
    "Не удалось распознать ответ. Выберите вариант на кнопке ниже или задайте вопрос о товаре "
    "(например: «расскажи про …», «что такое …»).\n\n"
)


def _grille_reask_current(session_id: str) -> ChatResponse:
    """Повторяет вопрос Smart Routing без изменения состояния сессии."""
    session = _get_session(session_id)
    phase = session["grille_phase"]
    subcats = session["allowed_subcats"]
    location = session["active_filters"].get("location", "")
    prefix = _GRILLE_INVALID_HINT
    if phase == "mount":
        mount_opts = _grille_mount_options(location, subcats)
        buttons = [ButtonOption(label=o["label"], value=o["value"]) for o in mount_opts]
        return ChatResponse(
            reply=prefix + "Как будет выполнен монтаж решетки?",
            action=ChatAction.ASK_QUESTION,
            buttons=buttons,
        )
    if phase == "feature":
        feat_opts = _grille_feature_options(subcats)
        buttons = [ButtonOption(label=o["label"], value=o["value"]) for o in feat_opts]
        return ChatResponse(
            reply=prefix + "Какие требования к решетке?",
            action=ChatAction.ASK_QUESTION,
            buttons=buttons,
        )
    return _grille_advance(session_id)


def _grille_handle_answer(session_id: str, message: str) -> ChatResponse:
    """Обрабатывает ответ на динамический шаг Smart Routing."""
    session = _get_session(session_id)
    phase = session["grille_phase"]
    location = session["active_filters"].get("location", "")
    subcats = session["allowed_subcats"]

    if phase == "mount":
        mount_opts = _grille_mount_options(location, subcats)
        valid = next(
            (o for o in mount_opts if o["value"] == message or o["label"] == message),
            None,
        )
        if valid is None:
            return _grille_reask_current(session_id)
        chosen = valid["value"]
        session["grille_routing"].append({
            "step": phase,
            "value": chosen,
            "subcats_before": list(session["allowed_subcats"]),
        })
        subcats = _filter_subcats_by_mount(session["allowed_subcats"], location, chosen)
        session["allowed_subcats"] = subcats
        session["grille_phase"] = "mount_done"
        return _grille_advance(session_id)

    if phase == "feature":
        feat_opts = _grille_feature_options(subcats)
        valid = next(
            (o for o in feat_opts if o["value"] == message or o["label"] == message),
            None,
        )
        if valid is None:
            return _grille_reask_current(session_id)
        chosen = valid["value"]
        session["grille_routing"].append({
            "step": phase,
            "value": chosen,
            "subcats_before": list(session["allowed_subcats"]),
        })
        subcats = _filter_subcats_by_feature(session["allowed_subcats"], chosen)
        session["allowed_subcats"] = subcats
        # Фильтр по характеристике с сайта: только Регулируемые или только Нерегулируемые
        if chosen == "adjustable":
            session["active_filters"]["regulated"] = "regulated"
        elif chosen == "fixed":
            session["active_filters"]["regulated"] = "fixed"
        else:
            session["active_filters"].pop("regulated", None)
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
            session["active_filters"].pop("regulated", None)
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

    get_collection()
    await ensure_catalog_ready()
    await asyncio.to_thread(warmup_embedding_and_search)

    sched = start_scheduler()
    yield
    sched.shutdown(wait=False)
    log.info("FastAPI-бэкенд остановлен.")


app = FastAPI(title="Бот-консультант ВРК", version="4.0.0", lifespan=lifespan)
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


def _message_content_to_str(content: Any) -> str:
    """LangChain может вернуть content как str или список блоков (мультимодальные модели)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            else:
                parts.append(str(block))
        return "\n".join(parts).strip()
    return str(content).strip()


async def _ask_llm(user_message: str, session_id: str, context: str) -> str:
    try:
        llm = get_llm()
    except RuntimeError as exc:
        log.error("LLM недоступен: %s", exc)
        return (
            "Не удалось обратиться к языковой модели — проверьте ключи в .env. "
            f"Телефон менеджера: {MANAGER_CONTACTS['phone']}"
        )
    session = _get_session(session_id)
    filters_text = _format_active_filters(session_id)
    system_msg = SystemMessage(
        content=SYSTEM_PROMPT.format(context=context, active_filters=filters_text)
    )
    history = session["history"][-20:]
    messages = [system_msg] + history + [HumanMessage(content=user_message)]
    try:
        response: AIMessage = await llm.ainvoke(messages)
        answer = _message_content_to_str(response.content)
    except Exception as exc:
        log.error("Ошибка LLM: %s", exc)
        answer = "Извините, произошла техническая ошибка. Попробуйте ещё раз или свяжитесь с менеджером."
    if not answer:
        log.warning("LLM вернул пустой ответ")
        answer = (
            "Не удалось сформулировать ответ. Уточните вопрос или "
            f"свяжитесь с менеджером: {MANAGER_CONTACTS['phone']}"
        )
    session["history"].append(HumanMessage(content=user_message))
    session["history"].append(AIMessage(content=answer))
    return answer


# ═══════════════════════════════════════════════════════════════════════════════
# ПОИСК С ФИЛЬТРАЦИЕЙ, ВАЛИДАЦИЕЙ И SUB_CATEGORY
# ═══════════════════════════════════════════════════════════════════════════════

# Ключи метаданных, которые реально хранятся в ChromaDB и попадают в where.
# Шаги воронки (slot_mount, ac_type, part_type, facade_* и т.д.) в where не идут —
# они только задают allowed_subcats. Сопоставление по сценариям: docs/SCENARIOS_FILTERS.md
METADATA_FILTER_KEYS = frozenset({
    "product_type", "location", "size_group", "material", "main_category",
    "regulated", "form", "scenario_block", "round_diameter_group", "installation",
})


def _get_scenario_block(active_filters: dict[str, str]) -> str | None:
    """
    Строит идентификатор блока сценария (как в scraper), чтобы искать только в нужном блоке БД.
    Возвращает None, если по фильтрам блок однозначно не определяется.
    """
    pt = active_filters.get("product_type") or ""
    loc = active_filters.get("location") or ""
    sg = active_filters.get("size_group") or ""
    form = (active_filters.get("form") or "").strip()
    if not pt or not sg:
        return None
    if loc == "duct":
        return f"grille_duct_{sg}" if pt == "grille" else f"{pt}_{loc}_{sg}"
    if pt == "grille":
        block = f"grille_{loc}_{sg}"
        if loc == "outdoor" and form:
            block += f"_{form}"
        return block
    if pt == "slot_grille":
        return f"slot_grille_{loc}_{sg}" if loc else None
    return f"{pt}_{loc}_{sg}"


def _filter_slot_grille_subcats(
    allowed_subcats: list[str], active_filters: dict[str, str],
) -> list[str]:
    """Оставляет только slug подкатегорий щелевых, совпадающие с выбранным типом монтажа/установки."""
    mount = active_filters.get("slot_mount")
    ceiling = (active_filters.get("slot_ceiling_type") or "").strip()
    if not mount:
        return allowed_subcats
    result = []
    for slug in allowed_subcats:
        rule = SLOT_GRILLE_SUBCAT_FILTER.get(slug)
        if not rule or rule.get("slot_mount") != mount:
            continue
        if mount == "concealed" and ceiling and rule.get("slot_ceiling_type") != ceiling:
            continue
        result.append(slug)
    return result if result else allowed_subcats


def _build_where_filter(
    active_filters: dict[str, str],
    allowed_subcats: list[str] | None = None,
) -> dict | None:
    conditions = []
    # Поиск в нужном блоке сценария: один фильтр по scenario_block сужает выборку
    scenario_block = _get_scenario_block(active_filters)
    if scenario_block:
        conditions.append({"scenario_block": {"$eq": scenario_block}})
    for k, v in active_filters.items():
        if not v or k not in METADATA_FILTER_KEYS:
            continue
        if k == "scenario_block":
            continue  # уже добавлен выше
        # «Нерегулируемая»: в where не добавляем (в выборку попадут и без характеристики), отбор — в _validate_product
        if k == "regulated" and v == "fixed":
            continue
        # «В воздуховод»: по location в метаданных не фильтруем, только по form=cylindrical
        if k == "location" and v == "duct":
            continue
        conditions.append({k: {"$eq": v}})
    if allowed_subcats:
        conditions.append({"category": {"$in": allowed_subcats}})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _validate_product(meta: dict, active_filters: dict[str, str]) -> bool:
    """Проверяет, что товар подходит под фильтры (учитываются только ключи из метаданных БД)."""
    for key, value in active_filters.items():
        if not value or key not in METADATA_FILTER_KEYS:
            continue
        product_value = meta.get(key, "") or ""
        # Регулируемая: только товары, у которых в характеристиках явно указано «Регулируемая».
        # Нерегулируемая: товары с «Нерегулируемая» или без указания регулировки (считаем нерегулируемыми).
        if key == "regulated":
            if value == "regulated":
                if (product_value or "").strip() != "regulated":
                    return False  # только явно регулируемые
            else:  # value == "fixed"
                if product_value and product_value not in ("", "fixed"):
                    return False  # исключаем только явно регулируемые
            continue
        # Форма: только товары с выбранной формой в характеристиках
        if key == "form":
            if (product_value or "").strip() != value:
                return False
            continue
        # «В воздуховод»: в метаданных по location не фильтруем
        if key == "location" and value == "duct":
            continue
        # installation: только товары с совпадающим способом монтажа (встраиваемая/накладная)
        if key == "installation":
            if (product_value or "").strip() != value:
                return False
            continue
        # scenario_block — производный, не проверяем по полю
        if key == "scenario_block":
            continue
        if product_value and product_value != value:
            return False
    return True


def _search_with_fallback(
    query: str,
    active_filters: dict[str, str],
    scenario: dict,
    allowed_subcats: list[str] | None = None,
    n_results: int = 8,
    detail_branch: str | None = None,
) -> list[dict]:
    where = _build_where_filter(active_filters, allowed_subcats)
    results = search(query, n_results=n_results, where=where)
    validated = [r for r in results if _validate_product(r.get("metadata", {}), active_filters)]
    if validated:
        return validated

    # Акустические решётки: не ослабляем фильтры — только подкатегория и выбранный материал
    if detail_branch == "acoustic":
        return validated  # уже пустой список

    relaxable = {k: v for k, v in active_filters.items() if v}
    # Круглые решётки: form и regulated не сбрасываем при fallback — иначе в выдачу попадут все подряд
    never_relax = set()
    if active_filters.get("form") == "round":
        never_relax = {"form", "regulated"}
    step_ids = list(reversed([s["step_id"] for s in scenario.get("steps", [])]))

    for key_to_relax in step_ids:
        if key_to_relax in never_relax:
            continue
        if key_to_relax in relaxable:
            relaxable.pop(key_to_relax)
            relaxed = _build_where_filter(relaxable, allowed_subcats)
            results = search(query, n_results=n_results, where=relaxed)
            validated = [r for r in results if _validate_product(r.get("metadata", {}), relaxable)]
            if validated:
                log.info("Fallback: убран фильтр '%s', найдено %d", key_to_relax, len(validated))
                return validated

    if allowed_subcats and detail_branch != "acoustic":
        relaxed = _build_where_filter(relaxable)
        results = search(query, n_results=n_results, where=relaxed)
        if results:
            log.info("Fallback: убраны subcategory фильтры, найдено %d", len(results))
            return results

    # Круглые: никогда не возвращать «любые» решётки — только с form=round или пусто
    if active_filters.get("form") == "round":
        minimal_where = {"$and": [
            {"form": {"$eq": "round"}},
            {"product_type": {"$eq": "grille"}},
        ]}
        results = search(query, n_results=n_results, where=minimal_where)
        validated = [r for r in results if _validate_product(r.get("metadata", {}), active_filters)]
        if validated:
            return validated
        return []

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


def _product_data_list(results: list[dict], n: int = 5) -> list[dict]:
    """Возвращает до n карточек товаров для выдачи списком (каждый в отдельном сообщении)."""
    out: list[dict] = []
    seen_urls: set[str] = set()
    for r in results[: n * 2]:
        if len(out) >= n:
            break
        meta = r.get("metadata", {})
        url = meta.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "name": meta.get("name", ""),
            "article": meta.get("article", ""),
            "price": meta.get("price", ""),
            "url": url,
            "category": meta.get("category", ""),
            "location": meta.get("location", ""),
        })
    return out


async def _do_filtered_search(session_id: str, user_message: str) -> ChatResponse:
    session = _get_session(session_id)
    scenario = _get_scenario(session_id)
    query = user_message or _build_search_query(session_id)
    text_probe = (user_message or "").strip() or query
    duct_direct_search = (
        session.get("scenario_key") == "grille"
        and (session.get("active_filters") or {}).get("location") == "duct"
    )
    subcats = session.get("allowed_subcats") or None
    if session.get("scenario_key") == "slot_grille" and subcats:
        subcats = _filter_slot_grille_subcats(subcats, session["active_filters"])
    # Детали систем вентиляции (адаптеры и др.): больше результатов — в подкатегориях много позиций
    n_results = 15 if session.get("scenario_key") == "vent_parts" else 8
    results = _search_with_fallback(query, session["active_filters"], scenario, subcats, n_results=n_results)
    where_dbg = _build_where_filter(session["active_filters"], subcats)
    suppressed_by_entity_match = False

    entities = extract_product_entities(text_probe)
    sk = session.get("scenario_key")
    analog = is_analog_or_similar_intent(text_probe)
    generic_cat = is_generic_catalog_query(text_probe, entities)
    specific = (
        is_specific_product_query(text_probe, entities)
        and not analog
        and not generic_cat
    )

    if specific and entities and not analog:
        log.info(
            "filtered search: specific product query | entities=%s | scenario=%s",
            entities,
            sk,
        )
        matched = filter_results_by_entities(results, entities)
        matched = filter_results_by_product_type(matched, sk)
        if not matched:
            matched = filter_results_by_entities(
                _search_with_fallback(
                    text_probe,
                    session["active_filters"],
                    scenario,
                    subcats,
                    n_results=40,
                ),
                entities,
            )
            matched = filter_results_by_product_type(matched, sk)
        if matched:
            results = rank_exact_or_near_exact_matches(matched, entities)
            log.info(
                "filtered search: entity match | n=%d | suppressing unrelated cards",
                len(results),
            )
        else:
            # Duct-ветка grille: если валидные результаты уже есть, не подавляем карточки из-за entity-check.
            if not (duct_direct_search and len(results) >= 1):
                suppressed_by_entity_match = True
                log.info(
                    "filtered search: no entity match | suppressing unrelated product cards | scenario=%s",
                    sk,
                )
                ctx = _build_context([])
                reply = await _ask_llm(
                    (
                        f"Запрос (поиск после воронки): {query}\n"
                        "Точного совпадения по названию/серии из запроса в выдаче нет. "
                        "Ответь кратко; не выдумывай товар."
                    ),
                    session_id,
                    ctx,
                )
                _reset_funnel(session_id)
                return ChatResponse(reply=reply.strip(), action=ChatAction.ASK_QUESTION)
            log.info(
                "filtered search: no entity match in duct direct search, keeping validated results | scenario=%s | n=%d",
                sk,
                len(results),
            )
    elif generic_cat:
        log.info(
            "filtered search: generic category query — multi-product OK | scenario=%s",
            sk,
        )

    log.info(
        "Поиск | scenario=%s | filters=%s | subcats=%s | results=%d",
        session.get("scenario_key", "?"),
        session["active_filters"],
        subcats[:5] if subcats else "all",
        len(results),
    )
    log.info(
        "duct search diagnostics | query=%s | where=%s | validated_count=%d | suppressed_by_entity_match=%s | duct_direct_search=%s",
        query,
        where_dbg,
        len(results),
        suppressed_by_entity_match,
        duct_direct_search,
    )

    context = _build_context(results)
    await _ask_llm(
        f"Клиент ищет: {query}. Подбери подходящие товары из контекста.",
        session_id, context,
    )
    n_products = 10 if session.get("scenario_key") == "vent_parts" else 5
    if specific and entities:
        n_products = min(3, max(1, len(results)))
    products = _product_data_list(results, n=n_products)
    _reset_funnel(session_id)
    if products:
        return ChatResponse(
            reply="Вот решетки которые вам могут подойти:",
            action=ChatAction.SHOW_PRODUCT,
            products=products,
        )
    return ChatResponse(
        reply="Под заданные параметры товаров не найдено. Рекомендую связаться с менеджером для индивидуального подбора.",
        action=ChatAction.CONTACT_MANAGER,
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

    # Порядок важен: специфичные первее общих
    if any(w in lower for w in ("адаптер", "шумоглушител", "камера статич")):
        filters["product_type"] = "vent_parts"
    elif any(w in lower for w in ("щелев",)):
        filters["product_type"] = "slot_grille"
    elif any(w in lower for w in ("корзин", "кондиционер", "кронштейн", "экран для конд")):
        filters["product_type"] = "ac_basket"
    elif any(w in lower for w in ("воздухораспределител", "воздухораздат")):
        filters["product_type"] = "distributor"
    elif "клапан" in lower and not any(w in lower for w in ("решетк", "решётк")):
        filters["product_type"] = "vent_parts"
    elif "диффузор" in lower:
        filters["product_type"] = "diffuser"
    elif any(w in lower for w in ("решетк", "решётк")):
        filters["product_type"] = "grille"
    elif any(w in lower for w in ("электропривод", "привод")):
        pass  # excluded
    elif any(w in lower for w in ("фильтр", "hepa")):
        pass  # excluded

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


def _detect_transfer_execution_hint(text: str) -> str:
    """Пытается определить исполнение переточной решетки из текста."""
    t = (text or "").lower()
    if not t:
        return ""
    if "акуст" in t:
        return "acoustic"
    if "пр-бр" in t or "без ответной рамк" in t or "без рамк" in t:
        return "no_frame"
    if "переточ" in t or "двер" in t or "перегород" in t:
        return "standard"
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# РАСПОЗНАВАНИЕ НАМЕРЕНИЙ (Intent Recognition)
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_intent(text: str) -> dict[str, bool]:
    """Распознаёт специальные намерения из INTENT_TRIGGERS."""
    lower = text.lower()
    intents: dict[str, bool] = {}
    for intent_key, triggers in INTENT_TRIGGERS.items():
        if any(t in lower for t in triggers):
            intents[intent_key] = True
    return intents


# ═══════════════════════════════════════════════════════════════════════════════
# ДЕТАЛЬНАЯ ВЕТКА (CSV decision tree)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_detail_steps(branch: str) -> list[dict]:
    if branch == "facade":
        return FACADE_STEPS
    if branch == "acoustic":
        return ACOUSTIC_STEPS
    if branch == "indoor":
        return INDOOR_STEPS
    if branch == "slot":
        return SLOT_STEPS
    return []


def _detail_step_applicable(step: dict, answers: dict) -> bool:
    """Проверяет condition шага и applicable_when_not — шаг пропускается, если не применим."""
    # Шаг показываем только когда НЕТ ответа из applicable_when_not (для круглых скрываем mount/construction/regulated)
    skip_if = step.get("applicable_when_not")
    if skip_if:
        for key, skip_val in skip_if.items():
            if answers.get(key) == skip_val:
                return False
    # Шаг показываем только когда condition выполнен (напр. facade_material только при form=round)
    cond = step.get("condition")
    if not cond:
        return True
    for key, required_val in cond.items():
        actual = answers.get(key)
        if isinstance(required_val, (list, tuple, set)):
            if actual not in required_val:
                return False
        elif actual != required_val:
            return False
    return True


def _next_detail_step(session_id: str) -> int | None:
    """Находит индекс следующего неотвеченного и применимого шага."""
    s = _get_session(session_id)
    steps = _get_detail_steps(s["detail_branch"])
    answers = s["detail_answers"]
    for i in range(s["detail_step_idx"], len(steps)):
        step = steps[i]
        if step["step_id"] not in answers and _detail_step_applicable(step, answers):
            return i
    return None


async def _detail_ask(session_id: str, prefix: str = "") -> ChatResponse:
    """Задаёт следующий вопрос из детальной ветки или завершает поиском."""
    s = _get_session(session_id)
    idx = _next_detail_step(session_id)
    if idx is None:
        return await _detail_search(session_id)

    s["detail_step_idx"] = idx
    s["funnel_phase"] = "detail"
    step = _get_detail_steps(s["detail_branch"])[idx]
    if s.get("detail_branch") == "facade":
        fsize = (s.get("detail_answers") or {}).get("facade_size")
        if fsize:
            log.info("facade detail: size selected = %s", fsize)
            if fsize == "under_2m2":
                log.info("facade detail: skipping reinforcement for under2m2")
            elif fsize == "over_2m2" and step.get("step_id") == "facade_reinforced_frame":
                log.info("facade detail: offering frame reinforcement only")
            elif fsize == "over_4m2" and step.get("step_id") in ("facade_reinforced_frame", "facade_reinforced_louvers"):
                log.info("facade detail: offering frame + louver reinforcement")
    log.info(
        "detail flow: asking step | branch=%s | step_id=%s | idx=%d",
        s.get("detail_branch"),
        step.get("step_id"),
        idx,
    )
    options = step.get("options", [])
    # Аккустические решётки: только Алюминий и Оцинкованная сталь (нержавеющей нет в ассортименте)
    if step.get("step_id") == "acoustic_material":
        options = [o for o in options if (o.get("value") or "") in ("aluminum", "galvanized")]
    answers = s.get("detail_answers", {})
    # Для накладной решётки шаг регулировки не показывается (applicable_when_not в config)
    buttons = [
        ButtonOption(
            label=opt["label"],
            value=opt.get("value") or opt["label"],
        )
        for opt in options
    ]

    hint = step.get("hint", "")
    if hint:
        prefix = f"💡 {hint}\n\n{prefix}" if prefix else f"💡 {hint}\n\n"

    return ChatResponse(
        reply=prefix + step["question"],
        action=ChatAction.ASK_QUESTION,
        buttons=buttons,
    )


def _recommend_series(session_id: str) -> str:
    """Подбирает рекомендуемые серии на основе ответов детальной ветки."""
    s = _get_session(session_id)
    branch = s["detail_branch"]
    answers = s["detail_answers"]
    intents = s.get("detected_intents", {})
    parts: list[str] = []

    if branch == "facade":
        solution = answers.get("facade_solution_type", "standard")
        size = answers.get("facade_size", "")
        if solution == "regulated":
            series = FACADE_SERIES.get("regulated", [])
        elif solution == "service":
            series = FACADE_SERIES.get("service", [])
        elif solution == "inertial":
            series = FACADE_SERIES.get("inertial", [])
        elif solution == "high_kzhs":
            kzhs_variant = answers.get("facade_high_kzhs_variant", "standard")
            kzhs_key = "high_kzhs_custom" if kzhs_variant == "custom" else "high_kzhs_standard"
            series = FACADE_SERIES.get(kzhs_key, [])
        else:
            if size == "under_2m2":
                series = FACADE_SERIES.get("standard_under_2m2", [])
            elif size == "over_2m2":
                series = FACADE_SERIES.get("standard_over_2m2", [])
            elif size == "over_4m2":
                mech = answers.get("facade_mechanical_vent", "no")
                priority = answers.get("facade_over4m2_priority", "")
                if mech == "yes" and priority == "rigidity":
                    series = FACADE_SERIES.get("standard_over_4m2_rigidity", [])
                else:
                    series = FACADE_SERIES.get("standard_over_4m2_price", [])
            else:
                series = FACADE_SERIES.get("standard_under_2m2", [])
        if series:
            parts.append(f"Рекомендуемые серии: {', '.join(series)}")
        if (
            solution == "standard"
            and size in ("over_4m2",)
            and intents.get("mechanical_vent")
        ):
            parts.append(SALES_ARGS["reinforced_recommendation"])

    elif branch == "indoor":
        indoor_type = answers.get("indoor_type", "")
        transfer_execution = answers.get("transfer_execution", "")
        if indoor_type == "transfer":
            transfer_series_map: dict[str, list[str]] = {
                "standard": ["ПР"],
                "no_frame": ["ПР-БР"],
                "acoustic": ["ПР-АКУСТИК"],
                "any": ["ПР", "ПР-БР", "ПР-АКУСТИК"],
            }
            series = transfer_series_map.get(transfer_execution, transfer_series_map["any"])
            parts.append(f"Рекомендуемые исполнения: {', '.join(series)}")
            return "\n".join(parts)
        priority = answers.get("indoor_priority", "")
        if priority in INDOOR_SERIES:
            series = INDOOR_SERIES[priority]
            parts.append(f"Рекомендуемые серии: {', '.join(series)}")
        if intents.get("budget"):
            parts.append("💰 Самый бюджетный вариант — серия АДЛ.")

    elif branch == "slot":
        mount = answers.get("slot_mount", "concealed")
        if mount in ("visible_frame", "visible"):
            series = SLOT_SERIES.get("visible_frame", [])
        else:
            ceiling = answers.get("slot_ceiling_type", "gkl")
            series = SLOT_SERIES.get(ceiling, [])
        if series:
            parts.append(f"Рекомендуемые серии: {', '.join(series)}")

    return "\n".join(parts)


async def _detail_search(session_id: str) -> ChatResponse:
    """Выполняет поиск после завершения детальной ветки."""
    s = _get_session(session_id)
    indoor_query_additions: list[str] = []
    if (
        s.get("detail_branch") == "slot"
        and (s.get("detail_answers") or {}).get("slot_mount") == "concealed"
        and (s.get("detail_answers") or {}).get("slot_ceiling_type") == "gkl"
    ):
        da = s.get("detail_answers") or {}
        if any(k not in da for k in SLOT_GKL_REQUIRED_KEYS):
            log.info(
                "detail flow: slot GKL required keys missing → continue asking | have=%s",
                list(da.keys()),
            )
            return await _detail_ask(session_id)
    log.info(
        "detail flow: branch completed → starting search | branch=%s | answers_keys=%s",
        s.get("detail_branch"),
        list((s.get("detail_answers") or {}).keys()),
    )
    # Подставляем в active_filters ответы из детальной ветки, используемые в метаданных ChromaDB
    if s.get("detail_branch") == "facade":
        answers = s.get("detail_answers") or {}
        solution = answers.get("facade_solution_type", "standard")
        regulated_val = answers.get("facade_regulated", "")

        # Привязка ответов фасадной ветки к фильтрам и подкатегориям (метаданные ChromaDB)
        if solution == "inertial":
            # Инерционная: только подкатегория «Инерционные» (category = reshetki-inertsionnye)
            s["allowed_subcats"] = [
                slug for slug, r in SUBCATEGORY_RULES.items()
                if r.get("feature") == "inertial"
            ]
            s["active_filters"]["regulated"] = "fixed"
            # Форму/тип монтажа для инерционных не фильтруем — в БД может не быть этих полей по этой подкатегории
            s["active_filters"].pop("form", None)
            s["active_filters"].pop("installation", None)
        elif solution == "regulated":
            s["active_filters"]["regulated"] = "regulated"
            s["active_filters"].pop("form", None)
            s["active_filters"].pop("installation", None)
            # Регулируемые по feature из каталога
            s["allowed_subcats"] = [
                slug for slug, r in SUBCATEGORY_RULES.items()
                if r.get("feature") == "adjustable"
            ] or s.get("allowed_subcats", [])
        elif solution == "service":
            s["active_filters"]["regulated"] = "fixed"
            s["active_filters"].pop("form", None)
            s["active_filters"].pop("installation", None)
            s["allowed_subcats"] = ["lyuki-ventilyacionnye"]
        elif solution == "high_kzhs":
            s["active_filters"]["regulated"] = "fixed"
            s["active_filters"].pop("form", None)
            s["active_filters"].pop("installation", None)
            # В каталоге повышенная жесткость ближе всего к сотовым решеткам
            s["allowed_subcats"] = [
                slug for slug, r in SUBCATEGORY_RULES.items()
                if r.get("feature") == "honeycomb"
            ] or s.get("allowed_subcats", [])
        elif answers.get("facade_form") == "round":
            # Круглые решётки для фасада: только наружные (не ВКР «в воздуховод»)
            s["active_filters"]["product_type"] = "grille"
            s["active_filters"]["location"] = "outdoor"
            s["active_filters"]["form"] = "round"
            s["active_filters"]["regulated"] = "fixed"
            s["active_filters"].pop("material", None)
            s["active_filters"].pop("round_diameter_group", None)
        else:
            # Обычные фасадные (встраиваемые/накладные): форма, материал, тип монтажа, регулировка
            form_val = answers.get("facade_form", "")
            if form_val:
                s["active_filters"]["form"] = form_val
            else:
                s["active_filters"].pop("form", None)
            mat = (answers.get("facade_material") or "").strip()
            if mat in ("aluminum", "galvanized", "stainless_steel"):
                s["active_filters"]["material"] = mat
            else:
                s["active_filters"].pop("material", None)
            # Накладные: только накладные решётки, регулируемых не бывает
            mount_type = answers.get("facade_mount_type", "")
            if mount_type in ("embedded", "surface"):
                s["active_filters"]["installation"] = mount_type
            else:
                s["active_filters"].pop("installation", None)
            if mount_type == "surface":
                s["active_filters"]["regulated"] = "fixed"
            elif regulated_val in ("regulated", "fixed"):
                s["active_filters"]["regulated"] = regulated_val
            else:
                s["active_filters"].pop("regulated", None)
            # allowed_subcats уже задан при входе в фасад (outdoor без инерционных) — не трогаем

    elif s.get("detail_branch") == "acoustic":
        # Акустические решётки: только подкатегория akusticheskie-reshetki и выбранный материал.
        # location и size_group не фильтруем — в каталоге у акустических часто outdoor/unknown и size_group unknown.
        answers = s.get("detail_answers") or {}
        s["allowed_subcats"] = ["akusticheskie-reshetki"]
        s["active_filters"]["product_type"] = "grille"
        s["active_filters"].pop("location", None)
        s["active_filters"].pop("size_group", None)
        mat = (answers.get("acoustic_material") or "").strip()
        if mat in ("aluminum", "galvanized"):
            s["active_filters"]["material"] = mat
        else:
            s["active_filters"].pop("material", None)
    elif s.get("detail_branch") == "indoor":
        answers = s.get("detail_answers") or {}
        indoor_type = (answers.get("indoor_type") or "").strip()
        transfer_execution = (answers.get("transfer_execution") or "").strip()
        indoor_priority = (answers.get("indoor_priority") or "").strip()
        indoor_filling = (answers.get("indoor_filling") or "").strip()
        if indoor_type == "transfer":
            s["allowed_subcats"] = ["reshetki-peretochnye"]
            s["active_filters"]["material"] = "aluminum"
            s["active_filters"].pop("regulated", None)
            transfer_query_hints = {
                "standard": "дверная переточная решетка пр",
                "no_frame": "переточная решетка пр-бр без ответной рамки",
                "acoustic": "звукопоглощающая переточная решетка пр-акустик",
                "any": "переточная решетка пр пр-бр пр-акустик",
            }
            indoor_query_additions.append(INDOOR_TYPE_QUERY_HINTS.get("transfer", ""))
            hint = transfer_query_hints.get(transfer_execution, transfer_query_hints["any"])
            if hint:
                indoor_query_additions.append(hint)
            log.info(
                "indoor transfer mapping: execution=%s | subcats=%s | material=%s",
                transfer_execution or "any",
                s.get("allowed_subcats"),
                s["active_filters"].get("material", ""),
            )
        else:
            current_subcats = s.get("allowed_subcats") or _filter_subcats_by_location("indoor")
            current_subcats = [
                slug
                for slug in current_subcats
                if "indoor" in SUBCATEGORY_RULES.get(slug, {}).get("location", [])
            ]
            narrowed = list(current_subcats)

            type_hints = INDOOR_TYPE_SUBCAT_HINTS.get(indoor_type, [])
            if type_hints:
                by_type = [slug for slug in narrowed if slug in type_hints]
                if by_type:
                    narrowed = by_type

            priority_hints = INDOOR_PRIORITY_SUBCAT_HINTS.get(indoor_priority, [])
            if priority_hints:
                by_priority = [slug for slug in narrowed if slug in priority_hints]
                if by_priority:
                    narrowed = by_priority

            if narrowed:
                s["allowed_subcats"] = narrowed

            # indoor_filling влияет на метаданный фильтр regulated (где применимо в каталоге).
            if indoor_filling in ("louvers", "deflector"):
                s["active_filters"]["regulated"] = "regulated"
            elif indoor_filling == "none":
                s["active_filters"]["regulated"] = "fixed"
            else:
                s["active_filters"].pop("regulated", None)

            for hint in (
                INDOOR_TYPE_QUERY_HINTS.get(indoor_type, ""),
                INDOOR_PRIORITY_QUERY_HINTS.get(indoor_priority, ""),
                INDOOR_FILLING_QUERY_HINTS.get(indoor_filling, ""),
            ):
                if hint:
                    indoor_query_additions.append(hint)

            log.info(
                "indoor detail mapping: type=%s | priority=%s | filling=%s | subcats=%s | regulated=%s",
                indoor_type or "-",
                indoor_priority or "-",
                indoor_filling or "-",
                (s.get("allowed_subcats") or [])[:6],
                s["active_filters"].get("regulated", ""),
            )

    recommendation = _recommend_series(session_id)
    query = _build_search_query(session_id)
    if indoor_query_additions:
        query += " " + " ".join(indoor_query_additions)
    if recommendation:
        query += " " + recommendation.split(":")[1].strip() if ":" in recommendation else ""

    scenario = _get_scenario(session_id)
    subcats = s.get("allowed_subcats") or None
    log.info(
        "Поиск (detail %s) | filters=%s | subcats=%s",
        s.get("detail_branch", "?"),
        s["active_filters"],
        subcats[:5] if subcats else "all",
    )
    results = _search_with_fallback(
        query, s["active_filters"], scenario, subcats,
        detail_branch=s.get("detail_branch"),
    )
    context = _build_context(results)

    extra_context = ""
    if recommendation:
        extra_context = f"\n\nРекомендация из ЧЕК-ЛИСТА менеджера:\n{recommendation}"

    await _ask_llm(
        f"Клиент ищет: {query}.{extra_context}\nПодбери товары из контекста.",
        session_id, context,
    )
    products = _product_data_list(results, n=5)
    _reset_funnel(session_id)
    if products:
        return ChatResponse(
            reply="Вот решетки которые вам могут подойти:",
            action=ChatAction.SHOW_PRODUCT,
            products=products,
        )
    return ChatResponse(
        reply="Под заданные параметры товаров не найдено. Рекомендую связаться с менеджером.",
        action=ChatAction.CONTACT_MANAGER,
    )


async def _after_main_scenario_completed(session_id: str, user_message: str = "") -> ChatResponse:
    """
    Все шаги FUNNEL_SCENARIOS для текущего scenario_key пройдены (кнопки или extracted).

    Дальше: либо detail branch (grille indoor/outdoor, slot_grille), либо прямой поиск.
    """
    session = _get_session(session_id)
    sk = session.get("scenario_key") or ""
    af = dict(session.get("active_filters") or {})
    scenario = _get_scenario(session_id)
    n_steps = len(scenario.get("steps", []))

    log.info(
        "scenario flow: main scenario steps completed | scenario_key=%s | step_idx=%s/%d | filters=%s",
        sk,
        session.get("step_idx"),
        n_steps,
        af,
    )

    if sk == "grille":
        loc = af.get("location", "")
        if loc == "indoor":
            subcats_now = session.get("allowed_subcats") or []
            transfer_only_subcats = bool(subcats_now) and all(
                slug == "reshetki-peretochnye" for slug in subcats_now
            )
            routing = session.get("grille_routing") or []
            transfer_from_routing = any(
                (item.get("step") in ("mount", "feature")) and item.get("value") == "transfer"
                for item in routing
            )
            transfer_from_filters = (
                af.get("grille_mount") == "transfer"
                or af.get("grille_feature") == "transfer"
            )
            transfer_only_indoor = (
                transfer_only_subcats
                or transfer_from_routing
                or transfer_from_filters
            )
            transfer_exec_hint = ""
            if transfer_only_indoor:
                transfer_exec_hint = (
                    _detect_transfer_execution_hint(user_message or "")
                    or (session.get("transfer_execution_hint") or "")
                )
            session["detail_branch"] = "indoor"
            session["detail_step_idx"] = 0
            if transfer_only_indoor:
                session["detail_answers"] = {"indoor_type": "transfer"}
                if transfer_exec_hint:
                    session["detail_answers"]["transfer_execution"] = transfer_exec_hint
                session["transfer_execution_hint"] = ""
            else:
                session["detail_answers"] = {}
                session["transfer_execution_hint"] = ""
            session["funnel_phase"] = "detail"
            if transfer_only_indoor:
                log.info(
                    "scenario flow: indoor transfer-only detected | prefill indoor_type=transfer, transfer_execution=%s | subcats=%s",
                    transfer_exec_hint or "-",
                    subcats_now[:4],
                )
            log.info("scenario flow: entering detail branch | branch=indoor")
            return await _detail_ask(session_id)
        if loc == "duct":
            log.info("scenario flow: grille duct → starting direct search (no detail)")
            q = (user_message or "").strip() or _build_search_query(session_id)
            return await _do_filtered_search(session_id, q)
        if loc == "outdoor":
            subcats = session.get("allowed_subcats") or _filter_subcats_by_location("outdoor")
            session["allowed_subcats"] = [
                s for s in subcats
                if SUBCATEGORY_RULES.get(s, {}).get("feature") != "inertial"
            ]
            session["detail_branch"] = "facade"
            session["detail_step_idx"] = 0
            session["detail_answers"] = {}
            session["funnel_phase"] = "detail"
            log.info("scenario flow: entering detail branch | branch=facade (outdoor path)")
            prefix = SALES_ARGS.get("embedded_vs_surface", "")
            return await _detail_ask(session_id, f"💡 {prefix}\n\n" if prefix else "")

    if sk == "slot_grille":
        session["detail_branch"] = "slot"
        session["detail_step_idx"] = 0
        prefill: dict[str, str] = {}
        sm = af.get("slot_mount", "")
        if sm:
            # В FUNNEL — filter_value «visible», в SLOT_STEPS — value «visible_frame»
            prefill["slot_mount"] = "visible_frame" if sm == "visible" else sm
        sct = af.get("slot_ceiling_type", "")
        if sct:
            prefill["slot_ceiling_type"] = sct
        session["detail_answers"] = prefill
        session["funnel_phase"] = "detail"
        log.info(
            "scenario flow: entering detail branch | branch=slot | prefill=%s",
            prefill,
        )
        return await _detail_ask(session_id)

    log.info(
        "scenario flow: starting direct search | scenario_key=%s (no detail branch for this scenario)",
        sk,
    )
    q = (user_message or "").strip() or _build_search_query(session_id)
    return await _do_filtered_search(session_id, q)


def _is_explicit_product_info_query(message: str) -> bool:
    """Явный запрос про товар (дополняет триггеры product_info)."""
    lower = message.lower().strip()
    if any(lower.startswith(p) for p in (
        "расскажи", "расскажите", "что такое", "чем отличается", "опиши",
        "характеристики", "информация про",
    )):
        return True
    if "что за " in lower and len(lower) < 120:
        return True
    return False


def _is_product_info_query(message: str) -> bool:
    lower = message.lower()
    for t in INTENT_TRIGGERS.get("product_info", []):
        if t in lower:
            return True
    return False


def _is_any_product_info_query(message: str) -> bool:
    return _is_product_info_query(message) or _is_explicit_product_info_query(message)


def _extract_product_subject(message: str) -> str:
    t = message.strip()
    for prefix in (
        "расскажи про", "расскажите про", "что такое", "что за",
        "чем отличается", "опиши", "описание", "информация про",
        "характеристики",
    ):
        if t.lower().startswith(prefix):
            t = t[len(prefix) :].strip(" :–-")
            break
    return t.strip()


def _strict_slot_gkl_incomplete(session_id: str) -> bool:
    s = _get_session(session_id)
    if s.get("funnel_phase") != "detail" or s.get("detail_branch") != "slot":
        return False
    da = s.get("detail_answers") or {}
    if da.get("slot_mount") != "concealed" or da.get("slot_ceiling_type") != "gkl":
        return False
    return any(k not in da for k in SLOT_GKL_REQUIRED_KEYS)


async def _handle_product_info(session_id: str, message: str) -> ChatResponse:
    s = _get_session(session_id)
    subject = _extract_product_subject(message) or message
    entities = extract_product_entities(message)
    analog = is_analog_or_similar_intent(message)
    specific = is_specific_product_query(message, entities) and not analog

    scenario = _get_scenario(session_id)
    af = dict(s.get("active_filters") or {})
    subcats = s.get("allowed_subcats") or None

    log.info(
        "product info: detected | specific=%s | analog=%s | entities=%s | subject=%s",
        specific,
        analog,
        entities,
        subject[:100],
    )

    results = _search_with_fallback(subject, af, scenario, subcats, n_results=20)
    matched: list[dict] = []

    if specific and entities:
        matched = filter_results_by_entities(results, entities)
        matched = filter_results_by_product_type(matched, s.get("scenario_key"))
        if not matched:
            matched = filter_results_by_entities(
                search(subject, n_results=50),
                entities,
            )
            matched = filter_results_by_product_type(matched, s.get("scenario_key"))
        if matched:
            results = rank_exact_or_near_exact_matches(matched, entities)[:12]
            log.info(
                "product info: exact/near-entity match | n=%d | suppressing unrelated cards",
                len(matched),
            )
        else:
            log.info(
                "product info: exact product match not found | suppressing unrelated product cards",
            )
            ctx = _build_context([])
            reply = await _ask_llm(
                (
                    f"Запрос: {message}\n"
                    "В предоставленном контексте нет карточки с точным совпадением "
                    "названия/серии из запроса. Ответь кратко: такой позиции в выгрузке не видно; "
                    "не придумывай характеристики. Предложи уточнить артикул или написать менеджеру."
                ),
                session_id,
                ctx,
            )
            return ChatResponse(
                reply=reply.strip(),
                action=ChatAction.ASK_QUESTION,
            )

    if not results:
        return ChatResponse(
            reply=(
                "В каталоге не нашлось подходящих позиций по запросу. "
                f"Уточните название или свяжитесь с менеджером: {MANAGER_CONTACTS['phone']}"
            ),
            action=ChatAction.CONTACT_MANAGER,
        )
    ctx = _build_context(results)
    low_ctx = ctx.lower()
    if "в базе знаний ничего не найдено" in low_ctx:
        return ChatResponse(
            reply=(
                "По этому запросу мало данных в каталоге. "
                f"Поможет менеджер: {MANAGER_CONTACTS['phone']}"
            ),
            action=ChatAction.CONTACT_MANAGER,
        )
    reply = await _ask_llm(
        (
            f"Запрос: {message}\n"
            "Ответь кратко (3–5 предложений) только по фактам из контекста. "
            "Если данных мало — скажи об этом."
        ),
        session_id,
        ctx,
    )
    if specific and entities:
        n_cards = min(3, len(results))
    else:
        n_cards = 5
    n_cards = min(n_cards, max(1, len(results)))
    products = _product_data_list(results, n=n_cards)
    if not products:
        return ChatResponse(
            reply=(
                "Не удалось сформировать карточки товаров по запросу. "
                f"Свяжитесь с менеджером: {MANAGER_CONTACTS['phone']}"
            ),
            action=ChatAction.CONTACT_MANAGER,
        )
    return ChatResponse(reply=reply.strip(), action=ChatAction.SHOW_PRODUCT, products=products)


async def _maybe_product_info_branch(session_id: str, message: str) -> ChatResponse | None:
    if not _is_any_product_info_query(message):
        return None
    # Обход запрещён: при незаполненных обязательных полях ГКЛ (скрытая щелевая) — только уточнение.
    if _strict_slot_gkl_incomplete(session_id):
        return await _detail_ask(session_id)
    return await _handle_product_info(session_id, message)


async def _handle_special_intent(session_id: str, message: str, intents: dict) -> ChatResponse | None:
    """Обрабатывает специальные намерения: аналог, нестандарт."""
    if intents.get("analog"):
        entities = extract_product_entities(message)
        wants_catalog_analog = is_analog_or_similar_intent(message) and (
            bool(entities) or len(message.strip()) >= 18
        )
        if wants_catalog_analog:
            log.info(
                "special intent: analog query -> related products | extracted entity=%s",
                entities,
            )
            s = _get_session(session_id)
            scenario = _get_scenario(session_id)
            af = dict(s.get("active_filters") or {})
            subcats = s.get("allowed_subcats") or None
            q = message.strip()
            results = _search_with_fallback(q, af, scenario, subcats, n_results=25)
            results = filter_results_by_product_type(results, s.get("scenario_key"))
            if not results:
                raw = search(q, n_results=40)
                results = filter_results_by_product_type(raw, s.get("scenario_key"))
            if results:
                ctx = _build_context(results)
                reply = await _ask_llm(
                    (
                        f"Клиент ищет аналог или похожую позицию: {message}\n"
                        "Ответь кратко по фактам из контекста; предложи варианты из списка."
                    ),
                    session_id,
                    ctx,
                )
                products = _product_data_list(results, n=min(5, len(results)))
                if products:
                    return ChatResponse(
                        reply=reply.strip(),
                        action=ChatAction.SHOW_PRODUCT,
                        products=products,
                    )
            log.info(
                "special intent: analog — no catalog hits, fallback to manager instructions",
            )
        return ChatResponse(
            reply=(
                f"🔍 **Подбор аналога**\n\n{SALES_ARGS['analog_instruction']}\n\n"
                "Пришлите артикул, фото или чертёж решетки, и мы подберём "
                "максимально близкий аналог из нашего ассортимента.\n\n"
                f"Или свяжитесь с менеджером: {MANAGER_CONTACTS['phone']}"
            ),
            action=ChatAction.CONTACT_MANAGER,
        )

    if intents.get("custom"):
        return ChatResponse(
            reply=(
                f"🏭 **Нестандартное изготовление**\n\n"
                f"{SALES_ARGS['custom_capabilities']}\n\n"
                f"• {SALES_ARGS['custom_frame_fast']}\n"
                f"• {SALES_ARGS['custom_frame_slow']}\n\n"
                f"Для расчёта свяжитесь с менеджером: {MANAGER_CONTACTS['phone']}"
            ),
            action=ChatAction.CONTACT_MANAGER,
        )

    return None


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

        if phase == "detail":
            idx = session["detail_step_idx"]
            if idx > 0:
                steps = _get_detail_steps(session["detail_branch"])
                prev_id = steps[idx - 1]["step_id"]
                session["detail_answers"].pop(prev_id, None)
                session["detail_step_idx"] = idx - 1
                log.info(
                    "detail flow: back → branch=%s | removed_step=%s | new_idx=%d",
                    session["detail_branch"],
                    prev_id,
                    session["detail_step_idx"],
                )
                return await _detail_ask(session_id)
            session["funnel_phase"] = "scenario"
            session["detail_branch"] = None
            session["detail_answers"] = {}
            # Возврат в сценарий: step_idx мог быть за пределами (например grille indoor → detail при step_idx=2)
            scenario = _get_scenario(session_id)
            steps = scenario["steps"]
            if session["step_idx"] >= len(steps):
                session["step_idx"] = max(0, len(steps) - 1)
            return _current_step_response(session_id)

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

    # ── Фаза: детальная ветка (CSV) ──
    if session["funnel_phase"] == "detail":
        branch = session["detail_branch"]
        steps = _get_detail_steps(branch)
        idx = session["detail_step_idx"]
        if idx < len(steps):
            step = steps[idx]
            chosen = None
            for opt in step.get("options", []):
                if opt.get("value") == message or opt["label"] == message:
                    chosen = opt.get("value", message)
                    break
            if chosen is not None:
                session["detail_answers"][step["step_id"]] = chosen
                session["detail_step_idx"] = idx + 1
                return await _detail_ask(session_id)

    # ── Фаза: выбор категории ──
    if session["funnel_phase"] == "product_type":
        for opt in PRODUCT_TYPE_STEP["options"]:
            if opt["filter_value"] == message or opt["label"] == message:
                return _activate_scenario(session_id, opt["filter_value"] or "_default")

    # ── Фаза: шаги сценария ──
    if session["funnel_phase"] == "scenario":
        scenario = _get_scenario(session_id)

        if scenario.get("dynamic") and session.get("grille_phase") in ("mount", "feature"):
            if _is_any_product_info_query(message):
                pinfo = await _maybe_product_info_branch(session_id, message)
                if pinfo is not None:
                    return pinfo
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

                # Корзины для кондиционеров: фильтр по подкатегориям (корзины / экраны·панели / кронштейны)
                if session.get("scenario_key") == "ac_basket" and current_step["step_id"] == "ac_type":
                    all_ac = [s for s, c in CATEGORY_SLUG_MAP.items() if c == "ac_basket"]
                    session["allowed_subcats"] = AC_BASKET_SUBCAT_FILTER.get(chosen_value, all_ac)

                # Воздухораспределители: фильтр по типу (панельные / низкоскоростные / дисковые / для чистых помещений)
                if session.get("scenario_key") == "distributor" and current_step["step_id"] == "distributor_type":
                    all_dist = [s for s, c in CATEGORY_SLUG_MAP.items() if c == "distributor"]
                    session["allowed_subcats"] = DISTRIBUTOR_SUBCAT_FILTER.get(chosen_value, all_dist)

                # Детали систем вентиляции: фильтр по типу (адаптеры / шумоглушители / воздушные клапаны)
                if session.get("scenario_key") == "vent_parts" and current_step["step_id"] == "part_type":
                    all_vp = [s for s, c in CATEGORY_SLUG_MAP.items() if c == "vent_parts"]
                    session["allowed_subcats"] = VENT_PARTS_SUBCAT_FILTER.get(chosen_value, all_vp)

                if scenario.get("dynamic") and current_step["step_id"] == "location":
                    # Сначала отдельные ветки (acoustic, outdoor, duct), чтобы не попасть в len(subcats)==0
                    if chosen_value == "acoustic" and session.get("scenario_key") == "grille":
                        session["detail_branch"] = "acoustic"
                        session["detail_step_idx"] = 0
                        session["detail_answers"] = {}
                        session["allowed_subcats"] = ["akusticheskie-reshetki"]
                        session["active_filters"]["product_type"] = "grille"
                        session["active_filters"]["location"] = "indoor"
                        return await _detail_ask(session_id)

                    subcats = _filter_subcats_by_location(chosen_value)
                    session["allowed_subcats"] = subcats

                    if len(subcats) == 0:
                        session["grille_phase"] = "done"
                        session["step_idx"] = 1
                        return _current_step_response(session_id)

                    # Grille outdoor → FACADE detail branch (только обычные фасадные, без инерционных)
                    if chosen_value == "outdoor" and session.get("scenario_key") == "grille":
                        session["detail_branch"] = "facade"
                        session["detail_step_idx"] = 0
                        session["detail_answers"] = {}
                        # Инерционные решётки — только при явном выборе; в ветке «Фасад» исключаем
                        session["allowed_subcats"] = [
                            s for s in session["allowed_subcats"]
                            if SUBCATEGORY_RULES.get(s, {}).get("feature") != "inertial"
                        ]
                        prefix = SALES_ARGS.get("embedded_vs_surface", "")
                        return await _detail_ask(session_id, f"💡 {prefix}\n\n" if prefix else "")

                    # Grille «В воздуховод» — только решётки с формой «Цилиндрические»
                    if chosen_value == "duct" and session.get("scenario_key") == "grille":
                        session["active_filters"]["form"] = "cylindrical"
                        session["grille_phase"] = "done"
                        session["step_idx"] = 1  # следующий шаг — size_group
                        session["allowed_subcats"] = list(SUBCATEGORY_RULES.keys())
                        return _current_step_response(session_id)

                    return _grille_advance(session_id)

                session["step_idx"] = idx + 1
                if session["step_idx"] < len(steps):
                    return _current_step_response(session_id)

                log.info(
                    "scenario flow: last FUNNEL step answered via buttons | session=%s",
                    session_id[:16],
                )
                return await _after_main_scenario_completed(session_id, message)

    # ── Распознавание намерений ──
    intents = analyze_intent(message)
    session["detected_intents"].update(intents)

    # Специальные намерения: аналог, нестандарт
    special = await _handle_special_intent(session_id, message, intents)
    if special:
        return special

    pinfo = await _maybe_product_info_branch(session_id, message)
    if pinfo:
        return pinfo

    # ── Умный анализ свободного текста ──
    extracted = _extract_filters_from_text(message)

    if extracted:
        pt = extracted.get("product_type") or session.get("scenario_key")
        scenario_key = pt or "_default"
        scenario = FUNNEL_SCENARIOS.get(scenario_key, FUNNEL_SCENARIOS["_default"])

        valid_filters, warnings = _validate_extracted(extracted, scenario, message)

        # Excluded categories
        excluded_pt = valid_filters.get("product_type", "")
        if excluded_pt and excluded_pt not in MAIN_CATEGORIES and excluded_pt != "_default":
            return ChatResponse(
                reply=(
                    "К сожалению, данная категория не входит в ассортимент, "
                    "поддерживаемый ботом. Я могу помочь с выбором вентиляционных "
                    "решеток, диффузоров, воздухораспределителей и других позиций.\n\n"
                    + PRODUCT_TYPE_STEP["question"]
                ),
                action=ChatAction.ASK_QUESTION,
                buttons=_make_buttons(PRODUCT_TYPE_STEP),
            )

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

        if session.get("scenario_key") == "grille":
            if "grille_mount" in extracted or "grille_feature" in extracted:
                _apply_grille_text_routing(session_id, extracted)
                narrowed_subcats = session.get("allowed_subcats") or []
                if narrowed_subcats and all(slug == "reshetki-peretochnye" for slug in narrowed_subcats):
                    session["active_filters"]["material"] = "aluminum"
                    transfer_hint = _detect_transfer_execution_hint(message)
                    if transfer_hint:
                        session["transfer_execution_hint"] = transfer_hint
            elif "location" in valid_filters:
                session["allowed_subcats"] = _filter_subcats_by_location(valid_filters["location"])

        # Mechanical vent trigger → inject sales arg
        if intents.get("mechanical_vent"):
            warnings.append(SALES_ARGS["mechanical_vent_warning"])

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

        # Все шаги сценария заполнены из текста — синхронизируем step_idx и уходим в тот же
        # маршрут, что и после последней кнопки (detail → search), иначе slot_grille и др.
        # преждевременно попадали в _do_filtered_search без detail-ветки.
        session["step_idx"] = len(steps)
        session["funnel_phase"] = "scenario"
        log.info(
            "scenario flow: extracted text filled all FUNNEL steps | scenario_key=%s | starting completion",
            session.get("scenario_key"),
        )
        return await _after_main_scenario_completed(session_id, message)

    # ── Триггеры начала воронки ──
    if _is_start_funnel(message) and session["funnel_phase"] is None:
        return _goto_main_menu(session_id)

    # ── Свободный вопрос (RAG) ──
    results = search(message, n_results=25)
    entities = extract_product_entities(message)
    sk_free = session.get("scenario_key")
    if is_analog_or_similar_intent(message):
        log.info("free RAG: analog query -> show related products (semantic)")
    elif is_generic_catalog_query(message, entities):
        log.info("free RAG: generic category query -> show product list")
    specific = is_specific_product_query(message, entities)
    matched_er: list[dict] = []

    if specific and entities:
        log.info(
            "free RAG: detected specific product query | entities=%s | semantic_hits=%d",
            entities,
            len(results),
        )
        matched_er = filter_results_by_entities(results, entities)
        matched_er = filter_results_by_product_type(matched_er, sk_free)
        if not matched_er:
            matched_er = filter_results_by_entities(
                search(_extract_product_subject(message) or message, n_results=50),
                entities,
            )
            matched_er = filter_results_by_product_type(matched_er, sk_free)
        if matched_er:
            results = rank_exact_or_near_exact_matches(matched_er, entities)
            context = _build_context(results)
            log.info(
                "free RAG: exact/near-exact match | n=%d | entity-filtered",
                len(results),
            )
        else:
            log.info(
                "free RAG: suppressing unrelated product cards (no entity match in metadata)",
            )
            context = _build_context([])
            llm_answer = await _ask_llm(
                (
                    f"{message}\n\n"
                    "Контекст каталога пуст для точного совпадения по названию/серии. "
                    "Ответь кратко: совпадения нет; не выдумывай товар."
                ),
                session_id,
                context,
            )
            return ChatResponse(
                reply=llm_answer.strip(),
                action=ChatAction.ASK_QUESTION,
            )
    else:
        if sk_free:
            filtered = filter_results_by_product_type(results, sk_free)
            if filtered:
                results = filtered
        context = _build_context(results)

    llm_answer = await _ask_llm(message, session_id, context)

    if session["funnel_phase"] in ("product_type", "scenario", "detail"):
        if session["funnel_phase"] == "product_type":
            step_cfg = PRODUCT_TYPE_STEP
        elif session["funnel_phase"] == "detail":
            idx = _next_detail_step(session_id)
            if idx is not None:
                branch_steps = _get_detail_steps(session["detail_branch"])
                step_cfg = branch_steps[idx]
                opts = step_cfg.get("options", [])
                if step_cfg.get("step_id") == "acoustic_material":
                    opts = [o for o in opts if (o.get("value") or "") in ("aluminum", "galvanized")]
                buttons = [
                    ButtonOption(label=o["label"], value=o.get("value") or o["label"])
                    for o in opts
                ]
                return ChatResponse(
                    reply=llm_answer + f"\n\n{step_cfg['question']}",
                    action=ChatAction.ASK_QUESTION,
                    buttons=buttons,
                )
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

    # Общая выдача: пачка карточек по семантике. Для запроса о конкретной модели —
    # только отфильтрованные по сущности (см. выше).
    if results:
        n_show = min(3 if (specific and entities) else 5, len(results))
        products = _product_data_list(results, n=n_show)
        if products:
            return ChatResponse(
                reply=(
                    "Вот подходящие позиции по запросу:"
                    if (specific and entities)
                    else "Вот решетки которые вам могут подойти:"
                ),
                action=ChatAction.SHOW_PRODUCT,
                products=products,
            )
    return ChatResponse(reply=llm_answer, action=ChatAction.ASK_QUESTION)


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
