"""
FastAPI-бэкенд бота-консультанта ООО "Завод ВРК".

Реализует:
- RAG-поиск по векторной базе товаров.
- Воронку продаж (State Machine) с хранением контекста по session_id.
- Единый эндпоинт /api/chat для веб-виджета и Telegram.
"""

from __future__ import annotations

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
    "funnel_step": None,    # None = свободный режим, str = текущий шаг воронки
    "funnel_data": {},      # собранные ответы воронки
    "history": [],          # история сообщений LangChain
})


def _get_session(session_id: str) -> dict[str, Any]:
    return _sessions[session_id]


def _reset_funnel(session_id: str) -> None:
    s = _get_session(session_id)
    s["funnel_step"] = None
    s["funnel_data"] = {}


# ─── Lifespan (запуск/остановка) ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Запуск FastAPI-бэкенда …")

    # Проверяем наличие LLM (при старте)
    try:
        get_llm()
    except RuntimeError as exc:
        log.critical(str(exc))

    # Проверяем / создаём коллекцию ChromaDB
    col = get_collection()
    if col.count() == 0:
        log.info("ChromaDB пуста — попытка индексации из raw_products.json …")
        reindex_all()

    # Планировщик
    sched = start_scheduler()

    yield

    sched.shutdown(wait=False)
    log.info("FastAPI-бэкенд остановлен.")


# ─── Приложение ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Бот-консультант ВРК",
    version="1.0.0",
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
        parts.append(f"--- Товар {i} ---\n{r['text']}")
    return "\n\n".join(parts)


async def _ask_llm(
    user_message: str,
    session_id: str,
    context: str,
) -> str:
    """Отправляет запрос к LLM с контекстом и историей."""
    llm = get_llm()
    session = _get_session(session_id)

    system_msg = SystemMessage(content=SYSTEM_PROMPT.format(context=context))

    # Ограничиваем историю последними 10 парами
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


# ─── Логика воронки ────────────────────────────────────────────────────────────

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
    filled = session["funnel_data"]
    for step_key in FUNNEL_ORDER:
        if step_key not in filled:
            return step_key
    return None


def _handle_funnel_answer(session_id: str, answer: str) -> None:
    """Сохраняет ответ клиента на текущий шаг воронки."""
    session = _get_session(session_id)
    current_step = session["funnel_step"]
    if current_step:
        session["funnel_data"][current_step] = answer


def _build_search_query(session_id: str) -> str:
    """Формирует поисковый запрос из накопленных данных воронки."""
    session = _get_session(session_id)
    parts = []
    for key, val in session["funnel_data"].items():
        step_conf = FUNNEL_STEPS.get(key, {})
        question = step_conf.get("question", key)
        parts.append(f"{question}: {val}")
    return " ".join(parts)


def _build_where_filter(session_id: str) -> dict | None:
    """Формирует фильтр метаданных ChromaDB из данных воронки."""
    session = _get_session(session_id)
    data = session["funnel_data"]
    conditions: list[dict] = []

    if "material" in data and data["material"] != "Не важно":
        conditions.append({"material": {"$contains": data["material"]}})
    if "location" in data and data["location"] != "Другое / не уверен":
        conditions.append({"location": {"$contains": data["location"].split("(")[0].strip()}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


# ─── Главный обработчик ───────────────────────────────────────────────────────

async def process_message(request: ChatRequest) -> ChatResponse:
    """Единая бизнес-логика обработки сообщения (веб + телеграм)."""
    session_id = request.session_id
    message = request.message.strip()
    session = _get_session(session_id)

    # --- Запрос связи с менеджером ---
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

    # --- Начало воронки ---
    if _is_start_funnel(message) and session["funnel_step"] is None:
        session["funnel_step"] = FUNNEL_ORDER[0]
        session["funnel_data"] = {}
        step = FUNNEL_STEPS[FUNNEL_ORDER[0]]
        buttons = [
            ButtonOption(label=opt, value=opt) for opt in step["options"]
        ]
        return ChatResponse(
            reply=step["question"],
            action=ChatAction.ASK_QUESTION,
            buttons=buttons,
        )

    # --- Продолжение воронки ---
    if session["funnel_step"] is not None:
        _handle_funnel_answer(session_id, message)

        next_step = _next_funnel_step(session_id)
        if next_step:
            session["funnel_step"] = next_step
            step = FUNNEL_STEPS[next_step]
            buttons = [
                ButtonOption(label=opt, value=opt) for opt in step["options"]
            ]
            return ChatResponse(
                reply=step["question"],
                action=ChatAction.ASK_QUESTION,
                buttons=buttons,
            )

        # Воронка завершена — поиск товара
        search_query = _build_search_query(session_id)
        where_filter = _build_where_filter(session_id)
        results = search(search_query, n_results=5, where=where_filter)

        if not results:
            results = search(search_query, n_results=5)

        context = _build_context(results)
        llm_answer = await _ask_llm(
            f"Клиент ищет: {search_query}. Подбери подходящие товары из контекста.",
            session_id,
            context,
        )

        product_data = None
        if results:
            best = results[0]["metadata"]
            product_data = {
                "name": best.get("name", ""),
                "article": best.get("article", ""),
                "price": best.get("price", ""),
                "url": best.get("url", ""),
                "category": best.get("category", ""),
            }

        _reset_funnel(session_id)

        return ChatResponse(
            reply=llm_answer,
            action=ChatAction.SHOW_PRODUCT if product_data else ChatAction.CONTACT_MANAGER,
            product_data=product_data,
        )

    # --- Свободный вопрос (RAG) ---
    results = search(message, n_results=5)
    context = _build_context(results)
    llm_answer = await _ask_llm(message, session_id, context)

    product_data = None
    action = ChatAction.ASK_QUESTION
    if results and results[0]["distance"] < 0.7:
        best = results[0]["metadata"]
        product_data = {
            "name": best.get("name", ""),
            "article": best.get("article", ""),
            "price": best.get("price", ""),
            "url": best.get("url", ""),
            "category": best.get("category", ""),
        }
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
