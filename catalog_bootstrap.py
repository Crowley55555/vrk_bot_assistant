"""
Первичное наполнение каталога при старте бэкенда.

Если raw_products.json отсутствует или пуст, а ChromaDB пуста — запускается парсер,
затем полная индексация. Не блокирует event loop: парсер асинхронный (httpx).
"""

from __future__ import annotations

import json

from config import (
    BOOTSTRAP_SCRAPER_ON_START,
    FORCE_SCRAPER_ON_START,
    RAW_PRODUCTS_PATH,
    REINDEX_ON_START,
)
from logger import get_logger
from scraper import run_delta_update
from vector_store import get_collection, reindex_all

log = get_logger(__name__)


def raw_products_ready() -> bool:
    """True, если raw_products.json есть и содержит непустой список товаров."""
    if not RAW_PRODUCTS_PATH.exists():
        return False
    try:
        raw = RAW_PRODUCTS_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return False
        data = json.loads(raw)
        if not isinstance(data, list):
            log.warning(
                "raw_products.json: ожидался JSON-массив, получен %s",
                type(data).__name__,
            )
            return False
        if len(data) == 0:
            return False
        return True
    except json.JSONDecodeError as exc:
        log.warning("raw_products.json: невалидный JSON — %s", exc)
        return False
    except OSError as exc:
        log.error("raw_products.json: ошибка чтения — %s", exc)
        return False


async def ensure_catalog_ready() -> None:
    """
    Вызывать после get_collection().

    Порядок: при FORCE — полный delta update и reindex; иначе если Chroma уже
    снабжена данными — выход без лишнего парсинга; если Chroma пуста — при
    необходимости парсинг, затем reindex_all().
    """
    n_before = get_collection().count()
    log.info(
        "═══ Bootstrap каталога: Chroma=%d док., raw готов=%s | "
        "FORCE_SCRAPER=%s, BOOTSTRAP_SCRAPER=%s, REINDEX_ON_START=%s ═══",
        n_before,
        raw_products_ready(),
        FORCE_SCRAPER_ON_START,
        BOOTSTRAP_SCRAPER_ON_START,
        REINDEX_ON_START,
    )

    if FORCE_SCRAPER_ON_START:
        log.info("FORCE_SCRAPER_ON_START: принудительный delta update …")
        try:
            report = await run_delta_update()
            log.info(
                "Парсер завершён: добавлено=%d, обновлено=%d, удалено из каталога=%d",
                len(report["added"]),
                len(report["updated"]),
                len(report["removed"]),
            )
        except Exception as exc:
            log.exception(
                "FORCE_SCRAPER: парсер завершился с ошибкой (сеть, DNS, HTTP, таймаут) — %s",
                exc,
            )
            if not raw_products_ready():
                log.error(
                    "Каталог не готов: raw_products.json отсутствует или пуст после ошибки парсера"
                )
                return
        if not REINDEX_ON_START:
            log.warning("REINDEX_ON_START=false — переиндексация после парсера пропущена")
            return
        if not raw_products_ready():
            log.error("После FORCE-парсера raw_products.json по-прежнему не готов")
            return
        n = reindex_all()
        if n == 0:
            log.error(
                "reindex_all() после FORCE вернул 0 документов — проверьте данные и process_to_chunks"
            )
        else:
            log.info("После FORCE_SCRAPER: в Chroma загружено %d документов", n)
        return

    if n_before > 0:
        if raw_products_ready():
            log.info(
                "Chroma (%d док.) и raw готовы — стартовый bootstrap не требуется",
                n_before,
            )
        else:
            log.warning(
                "Chroma содержит %d док., но raw_products.json отсутствует или пуст — "
                "поиск по RAG доступен; JSON будет восстановлен при следующем cron-обновлении",
                n_before,
            )
        return

    need_scrape = not raw_products_ready()

    if need_scrape and BOOTSTRAP_SCRAPER_ON_START:
        log.info(
            "Chroma пуста, raw_products.json отсутствует или пуст — "
            "запуск delta update (первичное наполнение) …"
        )
        try:
            report = await run_delta_update()
            log.info(
                "Парсер завершён: +%d новых, ~%d обновлено, -%d удалено",
                len(report["added"]),
                len(report["updated"]),
                len(report["removed"]),
            )
        except Exception as exc:
            log.exception(
                "Ошибка парсера при bootstrap (сеть, сайт, таймаут) — %s",
                exc,
            )
        if not raw_products_ready():
            log.error(
                "Bootstrap не удался: raw_products.json не создан, пуст или повреждён после парсера"
            )
            return

    elif need_scrape and not BOOTSTRAP_SCRAPER_ON_START:
        log.error(
            "Chroma пуста, raw_products.json нет, а BOOTSTRAP_SCRAPER_ON_START=false — "
            "включите переменную или загрузите raw_products.json в %s",
            RAW_PRODUCTS_PATH,
        )
        return

    if not REINDEX_ON_START:
        log.warning(
            "Chroma пуста, но REINDEX_ON_START=false — индексация отключена, поиск не будет работать"
        )
        return

    if not raw_products_ready():
        log.error(
            "Chroma пуста, индексировать нечего: raw_products.json не готов. "
            "Проверьте сеть и BOOTSTRAP_SCRAPER_ON_START."
        )
        return

    log.info("ChromaDB пуста — полная индексация из raw_products.json …")
    n = reindex_all()
    if n == 0:
        log.error(
            "reindex_all() вернул 0 при непустом raw — проверьте логи «Создано N чанков»"
        )
    else:
        log.info("Индексация завершена: %d документов в ChromaDB", n)
