"""
Pydantic-модели данных проекта.

Используются парсером, хранилищем и API-слоем
для единообразной валидации и сериализации.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ─── Товар (результат парсинга) ────────────────────────────────────────────────

class Product(BaseModel):
    """Одна карточка товара с сайта ВРК."""

    article: str = Field(..., description="Артикул (уникальный идентификатор)")
    name: str = Field(..., description="Название товара")
    url: str = Field(..., description="Полная ссылка на страницу товара")
    price: Optional[str] = Field(None, description="Цена (строка, напр. '1 500 ₽/шт')")
    old_price: Optional[str] = Field(None, description="Старая цена до акции")
    description: Optional[str] = Field(None, description="Текстовое описание со страницы товара")
    category: Optional[str] = Field(None, description="Категория / раздел каталога")
    characteristics: dict[str, str] = Field(
        default_factory=dict,
        description="Характеристики (материал, размеры, место применения и т.д.)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Метки: Хит, Акция, Новинка и т.д.",
    )
    content_hash: str = Field(
        default="",
        description="MD5-хеш ключевых полей для отслеживания изменений",
    )


# ─── API: запрос / ответ ──────────────────────────────────────────────────────

class ChatAction(str, Enum):
    ASK_QUESTION = "ask_question"
    SHOW_PRODUCT = "show_product"
    CONTACT_MANAGER = "contact_manager"


class ChatRequest(BaseModel):
    message: str
    session_id: str
    source: str = "web"  # web | telegram


class ButtonOption(BaseModel):
    label: str
    value: str


class ChatResponse(BaseModel):
    reply: str
    action: ChatAction = ChatAction.ASK_QUESTION
    product_data: Optional[dict] = None
    buttons: list[ButtonOption] = Field(default_factory=list)
