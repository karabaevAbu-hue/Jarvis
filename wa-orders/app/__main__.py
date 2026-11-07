"""Точка входа: python -m app"""
from __future__ import annotations

import asyncio
import logging
import sys

from filelock import FileLock, Timeout

from .config import DATA_DIR, LOG_DIR, ConfigError, load_config
from .logging_setup import setup_logging


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Замок берём первым, до логов: вторая копия не должна трогать ни лог, ни Telegram.
    lock = FileLock(str(DATA_DIR / "bot.lock"))
    try:
        lock.acquire(timeout=10)
    except Timeout:
        print("Бот уже запущен на этом компьютере (занят data/bot.lock).", file=sys.stderr)
        return 2

    try:
        try:
            cfg = load_config()
        except ConfigError as exc:
            print(f"Ошибка настройки: {exc}", file=sys.stderr)
            return 1
        handler = setup_logging(cfg.secrets())

        from aiogram.exceptions import TelegramUnauthorizedError

        from .service import run

        log = logging.getLogger(__name__)
        try:
            asyncio.run(run(cfg, handler))
        except KeyboardInterrupt:
            pass
        except TelegramUnauthorizedError:
            log.error("Telegram не принял TG_TOKEN. Проверьте токен от @BotFather в .env")
            print("Telegram не принял TG_TOKEN. Проверьте токен в .env", file=sys.stderr)
            return 1
        except Exception:
            # Через логгер, а не сырым traceback в консоль: так токены гарантированно замаскированы.
            log.exception("Сервис упал")
            print("Сервис упал, подробности в logs\\bot.log", file=sys.stderr)
            return 1
        log.info("Сервис остановлен")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
