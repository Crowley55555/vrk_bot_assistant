"""
Telegram-бот ООО "Завод ВРК" на aiogram 3.x.

Отдельный асинхронный процесс, который подключается
к единой бизнес-логике через process_message() из main.py.

Логика навигации:
- /start → СРАЗУ главное меню (первый шаг воронки), без промежуточного экрана.
  Приветственный текст устанавливается как описание бота (BotDescription),
  он виден пользователю ДО первого нажатия «Start» в Telegram.
- Первый шаг воронки = «Главное меню» → БЕЗ кнопок «Назад» / «Главное меню».
- Все последующие шаги → С кнопками «◀️ Назад» и «🏠 Главное меню».
"""

from __future__ import annotations

import asyncio
import re
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from config import (
    MANAGER_CONTACTS,
    PRODUCT_TYPE_STEP,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_WELCOME_TEXT,
)
from logger import get_logger
from main import process_message
from models import ButtonOption, ChatAction, ChatRequest, ChatResponse

log = get_logger(__name__)

router = Router()

_user_sessions: dict[int, str] = {}

_NAV_ROW = [
    InlineKeyboardButton(text="◀️ Назад", callback_data="__back__"),
    InlineKeyboardButton(text="🏠 Главное меню", callback_data="__main_menu__"),
]


def _session_id(user_id: int) -> str:
    if user_id not in _user_sessions:
        _user_sessions[user_id] = f"tg_{user_id}_{uuid.uuid4().hex[:8]}"
    return _user_sessions[user_id]


def _reset_session(user_id: int) -> None:
    _user_sessions.pop(user_id, None)


# ─── Reply-клавиатура (постоянная) ─────────────────────────────────────────────

_MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📞 Связаться с менеджером")]],
    resize_keyboard=True,
    is_persistent=True,
)


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def _is_main_menu(response: ChatResponse) -> bool:
    """Проверяет, является ли ответ главным меню (выбор категории)."""
    return response.reply == PRODUCT_TYPE_STEP["question"]


def _build_inline_keyboard(
    buttons: list[ButtonOption] | None = None,
    with_nav: bool = True,
    product_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Собирает Inline-клавиатуру: кнопки воронки + ссылка на товар + навигация."""
    rows: list[list[InlineKeyboardButton]] = []
    if buttons:
        for btn in buttons:
            cb_data = (btn.value or btn.label)[:64]
            rows.append([
                InlineKeyboardButton(text=btn.label, callback_data=cb_data)
            ])
    if product_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть на сайте", url=product_url)])
    if with_nav:
        rows.append(_NAV_ROW)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_product_card(data: dict) -> str:
    """Форматирует карточку товара для Telegram (без голых URL)."""
    parts = []
    if data.get("name"):
        parts.append(f"<b>{data['name']}</b>")
    if data.get("article"):
        parts.append(f"Артикул: {data['article']}")
    if data.get("price"):
        parts.append(f"💰 Цена: <b>{data['price']}</b>")
    if data.get("location"):
        loc_label = "наружное" if data["location"] == "outdoor" else "внутреннее"
        parts.append(f"Назначение: {loc_label}")
    return "\n".join(parts)


_URL_RE = re.compile(r"https?://\S+")


def _strip_bare_urls(text: str) -> str:
    """Убирает голые URL из текста (они дублируют кнопку-ссылку)."""
    return _URL_RE.sub("", text).strip()


async def _send_response(
    target: Message | CallbackQuery,
    response: ChatResponse,
) -> None:
    """
    Отправляет ответ бота в Telegram-чат.

    - Голые URL удаляются из текста LLM (ссылка идёт кнопкой).
    - Кнопка «🔗 Открыть на сайте» добавляется если есть product_data.url.
    - Навигация «Назад» / «Главное меню» — на всех шагах кроме главного меню.
    """
    if isinstance(target, CallbackQuery):
        send = target.message.answer
    else:
        send = target.answer

    text = _strip_bare_urls(response.reply)

    product_url = None
    if response.action == ChatAction.SHOW_PRODUCT and response.product_data:
        card = _format_product_card(response.product_data)
        text = f"{_strip_bare_urls(text)}\n\n{card}"
        product_url = response.product_data.get("url")

    show_nav = not _is_main_menu(response)
    inline_kb = _build_inline_keyboard(
        response.buttons if response.buttons else None,
        with_nav=show_nav,
        product_url=product_url,
    )

    await send(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=inline_kb,
    )


# ─── Обработчики ──────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """/start → сразу главное меню (первый шаг воронки)."""
    _reset_session(message.from_user.id)
    user_id = message.from_user.id
    session = _session_id(user_id)

    # Установить reply-клавиатуру «📞 Связаться с менеджером»
    await message.answer(
        "Для связи с менеджером нажмите кнопку ниже ↓",
        reply_markup=_MAIN_KEYBOARD,
    )

    # Сразу показать главное меню (первый шаг воронки)
    request = ChatRequest(message="Старт", session_id=session, source="telegram")
    response = await process_message(request)
    await _send_response(message, response)


@router.callback_query(F.data == "__back__")
async def cb_back(callback: CallbackQuery) -> None:
    """«◀️ Назад» — возврат на предыдущий шаг воронки."""
    await callback.answer()
    user_id = callback.from_user.id
    session = _session_id(user_id)

    request = ChatRequest(message="__back__", session_id=session, source="telegram")
    response = await process_message(request)
    await _send_response(callback, response)


@router.callback_query(F.data == "__main_menu__")
async def cb_main_menu(callback: CallbackQuery) -> None:
    """«🏠 Главное меню» — сброс и возврат к первому шагу воронки."""
    await callback.answer()
    user_id = callback.from_user.id
    _reset_session(user_id)
    session = _session_id(user_id)

    request = ChatRequest(message="__main_menu__", session_id=session, source="telegram")
    response = await process_message(request)
    await _send_response(callback, response)


@router.callback_query()
async def cb_funnel_step(callback: CallbackQuery) -> None:
    """Обработчик Inline-кнопок воронки (варианты ответа)."""
    await callback.answer()
    user_id = callback.from_user.id
    session = _session_id(user_id)
    chosen = callback.data or ""

    request = ChatRequest(message=chosen, session_id=session, source="telegram")
    response = await process_message(request)
    await _send_response(callback, response)


@router.message(F.text == "📞 Связаться с менеджером")
async def msg_contact_manager(message: Message) -> None:
    """Reply-кнопка «Связаться с менеджером»."""
    _reset_session(message.from_user.id)
    await message.answer(
        f"Свяжитесь с нашим менеджером:\n\n"
        f"📞 {MANAGER_CONTACTS['phone']}\n"
        f"📧 {MANAGER_CONTACTS['email']}\n"
        f"📍 {MANAGER_CONTACTS['address']}\n"
        f"🕐 {MANAGER_CONTACTS['work_hours']}",
        reply_markup=_build_inline_keyboard(with_nav=True),
    )


@router.message()
async def msg_free_text(message: Message) -> None:
    """Свободный текстовый ввод (вопрос/RAG)."""
    user_id = message.from_user.id
    session = _session_id(user_id)
    text = message.text or ""

    request = ChatRequest(message=text, session_id=session, source="telegram")
    response = await process_message(request)
    await _send_response(message, response)


# ─── Запуск бота ──────────────────────────────────────────────────────────────

async def run_bot() -> None:
    """Запуск Telegram-бота (long-polling)."""
    if not TELEGRAM_BOT_TOKEN:
        log.critical("TELEGRAM_BOT_TOKEN не задан в .env!")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Приветственный текст → описание бота (экран первого запуска в Telegram)
    try:
        await bot.set_my_description(description=TELEGRAM_WELCOME_TEXT)
        log.info("Описание бота установлено (экран первого запуска)")
    except Exception as exc:
        log.warning("Не удалось установить описание бота: %s", exc)

    log.info("Telegram-бот запущен (long-polling) …")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run_bot())
