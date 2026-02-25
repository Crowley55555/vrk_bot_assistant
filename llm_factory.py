"""
Фабрика LLM-провайдеров.

Проверяет наличие ключей в переменных окружения и инициализирует
клиент LangChain для первого найденного провайдера.

Приоритет: GigaChat → Yandex GPT → OpenRouter → OpenAI.

Единый интерфейс: get_llm() возвращает BaseChatModel, который
используется остальным кодом без привязки к конкретному провайдеру.
"""

from __future__ import annotations

import os
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

from logger import get_logger

log = get_logger(__name__)

_cached_llm: Optional[BaseChatModel] = None


# ─── Провайдеры ────────────────────────────────────────────────────────────────

def _try_gigachat() -> Optional[BaseChatModel]:
    """GigaChat (Сбер) через langchain-community."""
    credentials = os.getenv("GIGACHAT_CREDENTIALS")
    client_id = os.getenv("GIGACHAT_CLIENT_ID")
    client_secret = os.getenv("GIGACHAT_CLIENT_SECRET")

    if not credentials and not (client_id and client_secret):
        return None

    try:
        from langchain_community.chat_models.gigachat import GigaChat

        scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
        kwargs: dict = {"scope": scope, "verify_ssl_certs": False}

        if credentials:
            kwargs["credentials"] = credentials
        else:
            kwargs["client_id"] = client_id
            kwargs["client_secret"] = client_secret

        llm = GigaChat(**kwargs)
        log.info("LLM-провайдер: GigaChat (scope=%s)", scope)
        return llm
    except Exception as exc:
        log.warning("GigaChat: не удалось инициализировать — %s", exc)
        return None


def _try_yandex_gpt() -> Optional[BaseChatModel]:
    """Yandex GPT через langchain-community."""
    folder_id = os.getenv("YANDEX_FOLDER_ID")
    api_key = os.getenv("YANDEX_API_KEY")
    key_id = os.getenv("YANDEX_KEY_ID")

    if not folder_id:
        return None
    if not api_key and not key_id:
        return None

    try:
        from langchain_community.chat_models.yandex import ChatYandexGPT

        kwargs: dict = {"folder_id": folder_id}

        if api_key:
            kwargs["api_key"] = api_key
        else:
            kwargs["iam_token"] = _get_yandex_iam_token()

        llm = ChatYandexGPT(**kwargs)
        log.info("LLM-провайдер: Yandex GPT (folder=%s)", folder_id)
        return llm
    except Exception as exc:
        log.warning("Yandex GPT: не удалось инициализировать — %s", exc)
        return None


def _get_yandex_iam_token() -> str:
    """Получение IAM-токена Yandex через JWT сервисного аккаунта."""
    import time
    import json
    import jwt as pyjwt  # PyJWT
    import httpx

    key_id = os.environ["YANDEX_KEY_ID"]
    sa_id = os.environ["YANDEX_SERVICE_ACCOUNT_ID"]
    private_key = os.environ["YANDEX_PRIVATE_KEY"]

    now = int(time.time())
    payload = {
        "aud": "https://iam.api.cloud.yandex.net/iam/v1/tokens",
        "iss": sa_id,
        "iat": now,
        "exp": now + 3600,
    }
    encoded = pyjwt.encode(payload, private_key, algorithm="PS256", headers={"kid": key_id})

    resp = httpx.post(
        "https://iam.api.cloud.yandex.net/iam/v1/tokens",
        json={"jwt": encoded},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["iamToken"]


def _try_openrouter() -> Optional[BaseChatModel]:
    """OpenRouter — OpenAI-совместимый API."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None

    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        llm = ChatOpenAI(
            openai_api_key=api_key,
            openai_api_base="https://openrouter.ai/api/v1",
            model_name=model,
            temperature=0.3,
        )
        log.info("LLM-провайдер: OpenRouter (model=%s)", model)
        return llm
    except Exception as exc:
        log.warning("OpenRouter: не удалось инициализировать — %s", exc)
        return None


def _try_openai() -> Optional[BaseChatModel]:
    """OpenAI напрямую."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from langchain_openai import ChatOpenAI

        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        llm = ChatOpenAI(
            openai_api_key=api_key,
            model_name=model,
            temperature=0.3,
        )
        log.info("LLM-провайдер: OpenAI (model=%s)", model)
        return llm
    except Exception as exc:
        log.warning("OpenAI: не удалось инициализировать — %s", exc)
        return None


# ─── Публичный API ─────────────────────────────────────────────────────────────

_PROVIDERS = [
    ("GigaChat", _try_gigachat),
    ("Yandex GPT", _try_yandex_gpt),
    ("OpenRouter", _try_openrouter),
    ("OpenAI", _try_openai),
]


def get_llm() -> BaseChatModel:
    """
    Возвращает экземпляр LLM.

    Проверяет провайдеров по приоритету; результат кешируется.
    Если ни один провайдер не сконфигурирован — RuntimeError.
    """
    global _cached_llm
    if _cached_llm is not None:
        return _cached_llm

    for name, factory in _PROVIDERS:
        log.debug("Проверяю провайдер: %s …", name)
        llm = factory()
        if llm is not None:
            _cached_llm = llm
            return llm

    raise RuntimeError(
        "Ни один LLM-провайдер не сконфигурирован. "
        "Заполните .env — см. .env.example."
    )


def reset_llm_cache() -> None:
    """Сбросить кеш (полезно при смене ключей в рантайме)."""
    global _cached_llm
    _cached_llm = None
