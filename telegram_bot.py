"""
Telegram-бот ООО "Завод ВРК" на aiogram 3.x.

Отдельный асинхронный процесс, который подключается
к единой бизнес-логике через process_message() из main.py.

Все шаги воронки реализуются через Inline-кнопки (Callback queries).
Reply-кнопка «Связаться с менеджером» доступна на любом этапе.
"""

from __future__ import annotations

import asyncio
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

from config import MANAGER_CONTACTS, TELEGRAM_BOT_TOKEN, TELEGRAM_WELCOME_TEXT
from logger import get_logger
from main import process_message
from models import ButtonOption, ChatAction, ChatRequest, ChatResponse

log = get_logger(__name__)

router = Router()

# Связка Telegram user_id → session_id для сохранения контекста
_user_sessions: dict[int, str] = {}


def _session_id(user_id: int) -> str:
    """Возвращает (или создаёт) session_id для Telegram-пользователя."""
    if user_id not in _user_sessions:
        _user_sessions[user_id] = f"tg_{user_id}_{uuid.uuid4().hex[:8]}"
    return _user_sessions[user_id]


def _reset_session(user_id: int) -> None:
    """Сбрасывает сессию пользователя."""
    _user_sessions.pop(user_id, None)


# ─── Reply-клавиатура (постоянная) ─────────────────────────────────────────────

_MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📞 Связаться с менеджером")]],
    resize_keyboard=True,
    is_persistent=True,
)


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def _build_inline_keyboard(buttons: list[ButtonOption]) -> InlineKeyboardMarkup:
    """Создаёт Inline-клавиатуру из списка кнопок ответа."""
    rows: list[list[InlineKeyboardButton]] = []
    for btn in buttons:
        rows.append([
            InlineKeyboardButton(text=btn.label, callback_data=btn.value[:64])
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_product_card(data: dict) -> str:
    """Форматирует карточку товара для Telegram."""
    parts = []
    if data.get("name"):
        parts.append(f"<b>{data['name']}</b>")
    if data.get("article"):
        parts.append(f"Артикул: {data['article']}")
    if data.get("price"):
        parts.append(f"💰 Цена: <b>{data['price']}</b>")
    if data.get("url"):
        parts.append(f'🔗 <a href="{data["url"]}">Открыть на сайте</a>')
    return "\n".join(parts)


async def _send_response(
    target: Message | CallbackQuery,
    response: ChatResponse,
) -> None:
    """Отправляет ответ бота в Telegram-чат."""
    chat_id = target.from_user.id if target.from_user else 0

    # Если CallbackQuery — используем message.answer
    if isinstance(target, CallbackQuery):
        send = target.message.answer
    else:
        send = target.answer

    # Основной текст
    text = response.reply

    # Inline-кнопки
    inline_kb = None
    if response.buttons:
        inline_kb = _build_inline_keyboard(response.buttons)

    # Карточка товара
    if response.action == ChatAction.SHOW_PRODUCT and response.product_data:
        card = _format_product_card(response.product_data)
        text = f"{text}\n\n{card}"

    await send(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=inline_kb or _MAIN_KEYBOARD,
    )


# ─── Обработчики ──────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Обработчик команды /start — приветствие."""
    _reset_session(message.from_user.id)

    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Старт", callback_data="__start_funnel__")]
        ]
    )
    await message.answer(
        TELEGRAM_WELCOME_TEXT,
        reply_markup=inline_kb,
    )
    await message.answer(
        "Для связи с менеджером нажмите кнопку ниже ↓",
        reply_markup=_MAIN_KEYBOARD,
    )


@router.callback_query(F.data == "__start_funnel__")
async def cb_start_funnel(callback: CallbackQuery) -> None:
    """Нажатие Inline-кнопки «Старт» — начало воронки."""
    await callback.answer()
    user_id = callback.from_user.id
    session = _session_id(user_id)

    request = ChatRequest(message="Старт", session_id=session, source="telegram")
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
        reply_markup=_MAIN_KEYBOARD,
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

    log.info("Telegram-бот запущен (long-polling) …")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run_bot())
