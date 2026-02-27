"""
FastAPI-бэкенд бота-консультанта ООО "Завод ВРК".

Реализует:
- RAG-поиск по векторной базе товаров с **Metadata Filtering**.
- Воронку продаж (State Machine) с накоплением активных фильтров.
- Единый эндпоинт /api/chat для веб-виджета и Telegram.

При прохождении воронки каждый ответ пользователя маппится в filter_value,
который затем используется в ChromaDB where-clause для точной фильтрации.

─── ИНСТРУКЦИЯ ПО ТЕСТИРОВАНИЮ METADATA FILTERING ─────────────────────────────

1. Запустить парсер:
       python scheduler.py
   Убедиться, что в data/raw_products.json у товаров появились поля:
       "raw_attrs": {"Место применения": "На фасад", "Материал": "Алюминий", ...}
       "filters":   {"material": "metal", "location": "outdoor", ...}

2. Запустить бэкенд:
       uvicorn main:app --host 127.0.0.1 --port 8080 --reload

3. Пройти воронку через Telegram-бота или API:
   - Нажать «Старт»
   - Выбрать «Вентиляционные решетки» (grille)
   - Выбрать «Фасад / Улица» (outdoor)
   - Выбрать «Металл» (metal)
   - Выбрать размер

4. Проверить в логах (logs/bot.log):
   - Строку «Воронка завершена | filters=...» — должны быть выбранные фильтры
   - В результатах поиска ТОЛЬКО товары с location=outdoor.
     Товары с location=indoor должны быть ПОЛНОСТЬЮ ИСКЛЮЧЕНЫ.

5. Тест fallback: Если строгий фильтр вернул 0 результатов,
   система автоматически ослабит фильтры (уберёт менее важные) и повторит поиск.
   В логах появится строка «Fallback: убран фильтр '...'».
───────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from config import (
    FUNNEL_ORDER,
    FUNNEL_STEPS,
    FUNNEL_STEPS_MAP,
    MANAGER_CONTACTS,
    STATIC_DIR,
    SYSTEM_PROMPT,
)
from llm_factory import get_llm
from logger import get_logger
from models import ButtonOption, ChatAction, ChatRequest, ChatResponse
from scheduler import start_scheduler
from vector_store import get_collection, reindex_all, search

log = get_logger(__name__)

# ─── Хранилище сессий (in-memory) ─────────────────────────────────────────────

_sessions: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "funnel_step": None,       # None = свободный режим, str = текущий step_id
    "active_filters": {},      # step_id -> filter_value (накопленные фильтры)
    "history": [],             # история сообщений LangChain
})


def _get_session(session_id: str) -> dict[str, Any]:
    return _sessions[session_id]


def _reset_funnel(session_id: str) -> None:
    s = _get_session(session_id)
    s["funnel_step"] = None
    s["active_filters"] = {}


def _goto_main_menu(session_id: str) -> ChatResponse:
    """Сбрасывает воронку и возвращает первый шаг (= главное меню)."""
    _reset_funnel(session_id)
    session = _get_session(session_id)
    first_step_id = FUNNEL_ORDER[0]
    session["funnel_step"] = first_step_id
    step_config = FUNNEL_STEPS_MAP[first_step_id]
    return ChatResponse(
        reply=step_config["question"],
        action=ChatAction.ASK_QUESTION,
        buttons=_make_buttons(step_config),
    )


# ─── Lifespan (запуск/остановка) ───────────────────────────────────────────────

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


# ─── Приложение ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Бот-консультант ВРК",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Утилиты RAG / LLM ────────────────────────────────────────────────────────

def _build_context(results: list[dict]) -> str:
    """Собирает контекст из результатов поиска для промпта."""
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
            f"Фильтры: material={meta.get('material','?')}, "
            f"location={meta.get('location','?')}, "
            f"product_type={meta.get('product_type','?')}, "
            f"size_group={meta.get('size_group','?')}\n"
            f"Характеристики с сайта: {attrs_str}"
        )
    return "\n\n".join(parts)


def _format_active_filters(session_id: str) -> str:
    """Строковое представление активных фильтров для системного промпта."""
    session = _get_session(session_id)
    active = session.get("active_filters", {})
    if not active:
        return "Не заданы (свободный режим)"
    parts = []
    for step_id, value in active.items():
        if value:
            step_config = FUNNEL_STEPS_MAP.get(step_id, {})
            label = value
            for opt in step_config.get("options", []):
                if opt["filter_value"] == value:
                    label = f"{opt['label']} ({value})"
                    break
            parts.append(f"{step_id}: {label}")
        else:
            parts.append(f"{step_id}: не важно")
    return ", ".join(parts) if parts else "Все фильтры пропущены"


async def _ask_llm(
    user_message: str,
    session_id: str,
    context: str,
) -> str:
    """Отправляет запрос к LLM с контекстом, фильтрами и историей."""
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
        answer = (
            "Извините, произошла техническая ошибка. "
            "Попробуйте ещё раз или свяжитесь с менеджером."
        )

    session["history"].append(HumanMessage(content=user_message))
    session["history"].append(AIMessage(content=answer))

    return answer


# ─── Логика воронки с фильтрами ───────────────────────────────────────────────

def _is_start_funnel(message: str) -> bool:
    """Определяет, что пользователь хочет начать подбор."""
    triggers = [
        "старт", "начать", "подобрать", "помоги выбрать",
        "нужна решетка", "нужен диффузор", "хочу купить",
        "подбор", "каталог", "что есть",
    ]
    lower = message.lower().strip()
    return any(t in lower for t in triggers)


def _is_contact_request(message: str) -> bool:
    """Определяет запрос связи с менеджером."""
    triggers = [
        "менеджер", "связаться", "позвонить", "телефон",
        "контакт", "оператор", "человек",
    ]
    lower = message.lower().strip()
    return any(t in lower for t in triggers)


def _next_funnel_step(session_id: str) -> str | None:
    """Определяет следующий шаг воронки, который ещё не заполнен."""
    session = _get_session(session_id)
    completed = session["active_filters"]
    for step_id in FUNNEL_ORDER:
        if step_id not in completed:
            return step_id
    return None


def _handle_funnel_answer(session_id: str, answer: str) -> None:
    """
    Маппит ответ пользователя в filter_value и сохраняет в active_filters.

    Ищет совпадение по filter_value или label в опциях текущего шага.
    """
    session = _get_session(session_id)
    current_step = session["funnel_step"]
    if not current_step:
        return

    step_config = FUNNEL_STEPS_MAP.get(current_step)
    if not step_config:
        return

    filter_value = ""
    for opt in step_config["options"]:
        if opt["filter_value"] == answer or opt["label"] == answer:
            filter_value = opt["filter_value"]
            break

    session["active_filters"][current_step] = filter_value


def _make_buttons(step_config: dict) -> list[ButtonOption]:
    """Создаёт список кнопок для шага воронки."""
    return [
        ButtonOption(
            label=opt["label"],
            value=opt["filter_value"] if opt["filter_value"] else opt["label"],
        )
        for opt in step_config["options"]
    ]


def _build_search_query(session_id: str) -> str:
    """Формирует текстовый поисковый запрос из выбранных фильтров."""
    session = _get_session(session_id)
    parts = []
    for step_id, value in session["active_filters"].items():
        if not value:
            continue
        step_config = FUNNEL_STEPS_MAP.get(step_id)
        if step_config:
            for opt in step_config["options"]:
                if opt["filter_value"] == value:
                    parts.append(opt["label"])
                    break
    return " ".join(parts) if parts else "вентиляционное оборудование"


def _build_where_filter(session_id: str) -> dict | None:
    """
    Формирует ChromaDB where-clause из накопленных active_filters.

    Пустые значения (filter_value == "") пропускаются — частичная фильтрация.
    """
    session = _get_session(session_id)
    active = session["active_filters"]
    conditions: list[dict] = []

    for key, value in active.items():
        if value:
            conditions.append({key: {"$eq": value}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _search_with_fallback(
    query: str,
    session_id: str,
    n_results: int = 5,
) -> list[dict]:
    """
    Поиск с прогрессивным ослаблением фильтров.

    Если строгий поиск (все фильтры) не дал результатов,
    последовательно убираем менее важные фильтры (от конца FUNNEL_ORDER).
    """
    where_filter = _build_where_filter(session_id)
    results = search(query, n_results=n_results, where=where_filter)
    if results:
        return results

    session = _get_session(session_id)
    active = {k: v for k, v in session["active_filters"].items() if v}

    for key_to_relax in reversed(FUNNEL_ORDER):
        if key_to_relax in active:
            active.pop(key_to_relax)
            conditions = [{k: {"$eq": v}} for k, v in active.items()]
            relaxed_where = None
            if len(conditions) == 1:
                relaxed_where = conditions[0]
            elif len(conditions) > 1:
                relaxed_where = {"$and": conditions}

            results = search(query, n_results=n_results, where=relaxed_where)
            if results:
                log.info(
                    "Fallback: убран фильтр '%s', найдено %d результатов",
                    key_to_relax, len(results),
                )
                return results

    return search(query, n_results=n_results)


# ═══════════════════════════════════════════════════════════════════════════════
# УМНЫЙ АНАЛИЗ СВОБОДНОГО ТЕКСТА
# ═══════════════════════════════════════════════════════════════════════════════

_SIZE_RE = re.compile(r"(\d+)\s*[×хxXХ]\s*(\d+)")


def _extract_filters_from_text(text: str) -> dict[str, str]:
    """
    Извлекает фильтры из произвольного текста пользователя.

    Пример: «подбери решетку для квартиры в потолок 300х500»
    → {"product_type": "grille", "location": "indoor", "size_group": "small"}

    Возвращает только те фильтры, которые удалось уверенно определить.
    """
    lower = text.lower()
    filters: dict[str, str] = {}

    # product_type
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

    # location
    if any(w in lower for w in (
        "фасад", "улиц", "наружн", "уличн", "снаружи", "внешн",
    )):
        filters["location"] = "outdoor"
    elif any(w in lower for w in (
        "помещен", "внутр", "квартир", "офис", "потолок", "потолоч",
        "стен", "комнат", "дом", "кухн", "ванн", "туалет",
        "в пол", "наполн", "межкомнат", "переточн",
    )):
        filters["location"] = "indoor"

    # material
    if any(w in lower for w in (
        "металл", "сталь", "стальн", "алюмини", "нержавейк",
        "нержавеющ", "оцинков", "железн", "латун",
    )):
        filters["material"] = "metal"
    elif any(w in lower for w in ("пластик", "пластмасс", "пвх", "полипропилен")):
        filters["material"] = "plastic"
    elif any(w in lower for w in ("дерев", "деревянн", "мдф", "шпон")):
        filters["material"] = "wood"

    # size_group
    m = _SIZE_RE.search(text)
    if m:
        max_side = max(int(m.group(1)), int(m.group(2)))
        filters["size_group"] = "small" if max_side < 1000 else "large"
    elif any(w in lower for w in ("маленьк", "небольш", "компактн", "мини")):
        filters["size_group"] = "small"
    elif any(w in lower for w in ("больш", "крупн", "промышленн")):
        filters["size_group"] = "large"

    return filters


def _is_known_option(message: str) -> bool:
    """Проверяет, совпадает ли сообщение с одной из кнопок воронки."""
    for step in FUNNEL_STEPS:
        for opt in step["options"]:
            if opt["filter_value"] == message or opt["label"] == message:
                return True
    return False


def _describe_extracted(extracted: dict[str, str]) -> str:
    """Формирует человекочитаемое описание извлечённых фильтров."""
    parts: list[str] = []
    for step_id, value in extracted.items():
        step_config = FUNNEL_STEPS_MAP.get(step_id)
        if step_config:
            for opt in step_config["options"]:
                if opt["filter_value"] == value:
                    parts.append(opt["label"])
                    break
            else:
                parts.append(value)
    return ", ".join(parts)


def _best_product_data(results: list[dict]) -> dict | None:
    """Извлекает данные лучшего товара из результатов поиска."""
    if not results:
        return None
    best = results[0]["metadata"]
    return {
        "name": best.get("name", ""),
        "article": best.get("article", ""),
        "price": best.get("price", ""),
        "url": best.get("url", ""),
        "category": best.get("category", ""),
        "material": best.get("material", ""),
        "location": best.get("location", ""),
    }


async def _do_filtered_search(
    session_id: str,
    user_message: str,
) -> ChatResponse:
    """Выполняет поиск с текущими active_filters и формирует ответ через LLM."""
    session = _get_session(session_id)
    search_query = user_message or _build_search_query(session_id)
    results = _search_with_fallback(search_query, session_id)

    log.info(
        "Поиск с фильтрами | filters=%s | query='%s' | results=%d",
        session["active_filters"],
        search_query[:80],
        len(results),
    )

    context = _build_context(results)
    llm_answer = await _ask_llm(
        f"Клиент ищет: {search_query}. Подбери подходящие товары из контекста.",
        session_id,
        context,
    )

    product_data = _best_product_data(results)
    _reset_funnel(session_id)

    return ChatResponse(
        reply=llm_answer,
        action=ChatAction.SHOW_PRODUCT if product_data else ChatAction.CONTACT_MANAGER,
        product_data=product_data,
    )


# ─── Главный обработчик ───────────────────────────────────────────────────────

async def process_message(request: ChatRequest) -> ChatResponse:
    """
    Единая бизнес-логика обработки сообщения (веб + телеграм).

    Поддерживает три режима ввода:
    1. Нажатие кнопки — классический проход по воронке.
    2. Свободный текст с описанием товара — извлечение фильтров из текста,
       автозаполнение известных параметров, уточнение недостающих.
    3. Вопрос не о товаре — RAG-ответ без фильтрации.
    """
    session_id = request.session_id
    message = request.message.strip()
    session = _get_session(session_id)

    # ── Навигация ──
    if message == "__main_menu__":
        return _goto_main_menu(session_id)

    if message == "__back__":
        current_step = session["funnel_step"]
        if current_step and current_step in FUNNEL_ORDER:
            idx = FUNNEL_ORDER.index(current_step)
            if idx > 0:
                prev_step_id = FUNNEL_ORDER[idx - 1]
                session["active_filters"].pop(prev_step_id, None)
                session["funnel_step"] = prev_step_id
                step_config = FUNNEL_STEPS_MAP[prev_step_id]
                return ChatResponse(
                    reply=step_config["question"],
                    action=ChatAction.ASK_QUESTION,
                    buttons=_make_buttons(step_config),
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

    # ── Нажатие кнопки воронки (точное совпадение) ──
    if _is_known_option(message):
        if session["funnel_step"] is not None:
            _handle_funnel_answer(session_id, message)
        else:
            for step in FUNNEL_STEPS:
                for opt in step["options"]:
                    if opt["filter_value"] == message or opt["label"] == message:
                        session["active_filters"][step["step_id"]] = opt["filter_value"]
                        break

        next_step = _next_funnel_step(session_id)
        if next_step:
            session["funnel_step"] = next_step
            step_config = FUNNEL_STEPS_MAP[next_step]
            return ChatResponse(
                reply=step_config["question"],
                action=ChatAction.ASK_QUESTION,
                buttons=_make_buttons(step_config),
            )
        return await _do_filtered_search(session_id, _build_search_query(session_id))

    # ── Извлечение фильтров из свободного текста ──
    extracted = _extract_filters_from_text(message)

    if extracted:
        for key, value in extracted.items():
            session["active_filters"][key] = value

        next_step = _next_funnel_step(session_id)
        if next_step:
            session["funnel_step"] = next_step
            step_config = FUNNEL_STEPS_MAP[next_step]
            understood = _describe_extracted(extracted)
            prefix = f"✅ Понял: {understood}.\n\n" if understood else ""
            return ChatResponse(
                reply=prefix + step_config["question"],
                action=ChatAction.ASK_QUESTION,
                buttons=_make_buttons(step_config),
            )
        return await _do_filtered_search(session_id, message)

    # ── Триггеры начала воронки (без фильтров в тексте) ──
    if _is_start_funnel(message) and session["funnel_step"] is None:
        return _goto_main_menu(session_id)

    # ── Свободный вопрос (RAG) ──
    # Если пользователь в воронке, но написал вопрос не по теме подбора —
    # отвечаем через RAG и показываем текущий шаг воронки для продолжения.
    results = search(message, n_results=5)
    context = _build_context(results)
    llm_answer = await _ask_llm(message, session_id, context)

    if session["funnel_step"] is not None:
        step_config = FUNNEL_STEPS_MAP[session["funnel_step"]]
        return ChatResponse(
            reply=llm_answer + f"\n\n{step_config['question']}",
            action=ChatAction.ASK_QUESTION,
            buttons=_make_buttons(step_config),
        )

    product_data = None
    action = ChatAction.ASK_QUESTION
    if results and results[0]["distance"] < 0.7:
        product_data = _best_product_data(results)
        action = ChatAction.SHOW_PRODUCT

    return ChatResponse(
        reply=llm_answer,
        action=action,
        product_data=product_data,
    )


# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest) -> ChatResponse:
    """Универсальный эндпоинт чата для веб-виджета и Telegram."""
    log.info(
        "Запрос [%s] session=%s: %s",
        request.source,
        request.session_id[:8],
        request.message[:100],
    )
    response = await process_message(request)
    log.info(
        "Ответ [%s] action=%s: %s",
        request.source,
        response.action.value,
        response.reply[:100],
    )
    return response


@app.get("/health")
async def health_check() -> dict:
    """Проверка статуса системы."""
    col = get_collection()
    llm_ok = True
    try:
        get_llm()
    except RuntimeError:
        llm_ok = False

    return {
        "status": "ok",
        "llm_available": llm_ok,
        "chroma_documents": col.count(),
    }


# ─── Точка входа ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
