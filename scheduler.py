"""
Планировщик автоматического обновления данных.

Запускает парсинг по расписанию (по умолчанию — каждый понедельник в 03:00)
и обновляет векторную базу ChromaDB.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import SCRAPER_CRON_DAY_OF_WEEK, SCRAPER_CRON_HOUR, SCRAPER_CRON_MINUTE
from logger import get_logger
from scraper import process_to_chunks, run_delta_update
from vector_store import index_chunks, remove_by_ids

log = get_logger(__name__)

scheduler = AsyncIOScheduler()


async def scheduled_scrape_job() -> None:
    """
    Задача планировщика: парсинг + обновление ChromaDB.

    1. Запускает Delta Update (парсер).
    2. Генерирует чанки из обновлённых данных.
    3. Обновляет векторную базу (upsert + удаление).
    """
    log.info("═══ Начало запланированного обновления данных ═══")
    try:
        report = await run_delta_update()

        changed_ids = report["added"] + report["updated"]
        if changed_ids:
            chunks = process_to_chunks()
            relevant = [c for c in chunks if c["id"] in set(changed_ids)]
            index_chunks(relevant)
            log.info("Проиндексировано %d новых/изменённых товаров", len(relevant))

        if report["removed"]:
            remove_by_ids(report["removed"])
            log.info("Удалено %d товаров из индекса", len(report["removed"]))

        log.info(
            "═══ Обновление завершено: +%d, ~%d, -%d, =%d ═══",
            len(report["added"]),
            len(report["updated"]),
            len(report["removed"]),
            len(report["unchanged"]),
        )
    except Exception as exc:
        log.exception("Ошибка при запланированном обновлении: %s", exc)


def start_scheduler() -> AsyncIOScheduler:
    """Настраивает и запускает планировщик APScheduler."""
    trigger = CronTrigger(
        day_of_week=SCRAPER_CRON_DAY_OF_WEEK,
        hour=SCRAPER_CRON_HOUR,
        minute=SCRAPER_CRON_MINUTE,
    )
    scheduler.add_job(
        scheduled_scrape_job,
        trigger=trigger,
        id="weekly_scrape",
        replace_existing=True,
        name="Еженедельный парсинг каталога ВРК",
    )
    scheduler.start()
    log.info(
        "Планировщик запущен: %s в %02d:%02d",
        SCRAPER_CRON_DAY_OF_WEEK,
        SCRAPER_CRON_HOUR,
        SCRAPER_CRON_MINUTE,
    )
    return scheduler


# ─── CLI: ручной запуск ────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _manual_run():
        log.info("Ручной запуск парсера …")
        await scheduled_scrape_job()

    asyncio.run(_manual_run())
