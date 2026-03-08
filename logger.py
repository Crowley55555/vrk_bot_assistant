"""
Единая настройка логирования для всех модулей проекта.

Логи пишутся одновременно в файл (logs/bot.log) и в консоль.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import LOG_FILE, LOG_LEVEL

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024,  # 10 МБ
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FMT))

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FMT))


def get_logger(name: str) -> logging.Logger:
    """Возвращает логгер с общими обработчиками."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(LOG_LEVEL)
        logger.addHandler(_file_handler)
        logger.addHandler(_console_handler)
        logger.propagate = False
    return logger
