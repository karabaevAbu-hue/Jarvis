"""Логи: файл с ротацией, токены всегда замаскированы."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR

# Токен GREEN-API стоит прямо в адресе запроса: /waInstance<id>/<метод>/<токен>
_GREEN_URL = re.compile(r"(/waInstance\d+/[A-Za-z]+/)[A-Za-z0-9]{16,}")
_TG_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def mask_secrets(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, secret[:4] + "***")
    text = _GREEN_URL.sub(r"\1***", text)
    return _TG_TOKEN.sub("***:***", text)


class MaskingFormatter(logging.Formatter):
    """Маскирует токены во всём тексте записи, включая traceback."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self._secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record), self._secrets)


def setup_logging(secrets: list[str], *, console: bool | None = None) -> RotatingFileHandler:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    formatter = MaskingFormatter(fmt, secrets)

    file_handler = RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8", delay=True
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)

    # В консоль пишем только при ручном запуске. Под службой stdout уходит в service.log,
    # и дубль там не нужен.
    if console is None:
        console = bool(sys.stdout and sys.stdout.isatty())
    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # httpx на INFO пишет полный адрес каждого запроса — вместе с токеном GREEN-API.
    for noisy in ("httpx", "httpcore", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return file_handler
