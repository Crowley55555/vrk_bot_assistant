#!/usr/bin/env python3
"""
Сброс данных под ноль: удаление ChromaDB и raw_products.json.

Парсер и индексацию запускаете отдельно — тогда всё соберётся заново.

Запуск (при остановленном боте/API):
    python full_reset.py
"""

from __future__ import annotations

import sys

from config import RAW_PRODUCTS_PATH
from logger import get_logger
from vector_store import reset_db

log = get_logger(__name__)


def main() -> int:
    log.info("Сброс данных под ноль…")
    reset_db()
    if RAW_PRODUCTS_PATH.exists():
        RAW_PRODUCTS_PATH.unlink()
        log.info("Удалён %s", RAW_PRODUCTS_PATH)
    else:
        log.info("Файл %s отсутствовал", RAW_PRODUCTS_PATH)
    log.info("Готово. Запустите парсер — данные соберутся заново.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
