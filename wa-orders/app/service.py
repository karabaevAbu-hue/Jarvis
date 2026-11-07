"""Сборка и запуск всех частей бота."""
from __future__ import annotations

import asyncio
import logging
import time
import zipfile
from datetime import datetime
from logging import Handler

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError

from .bot import Deps, Notifier, build_router
from .config import BACKUP_DIR, Config
from .db import Database
from .engine import Engine, Runtime, day_start
from .greenapi import GreenAPI, GreenAPIError
from .watchdog import Watchdog, ensure_instance_settings

log = logging.getLogger(__name__)

KEEP_BACKUPS = 14
KEEP_LOG_DAYS = 90


async def make_backup(db: Database, day: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    raw = BACKUP_DIR / f"bot-{day}.db"
    await db.backup_to(raw)

    def _zip() -> None:
        with zipfile.ZipFile(BACKUP_DIR / f"bot-{day}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(raw, raw.name)
        raw.unlink(missing_ok=True)
        for old in sorted(BACKUP_DIR.glob("bot-*.zip"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)

    await asyncio.to_thread(_zip)


async def maintenance(db: Database, notifier: Notifier, cfg: Config) -> None:
    """Раз в сутки: бэкап базы после 05:00 и итог дня в 21:00."""
    while True:
        await asyncio.sleep(60)
        try:
            local = datetime.now(cfg.tz)
            today = local.date().isoformat()
            if local.hour >= 5 and await db.get("last_backup_day") != today:
                await make_backup(db, today)
                await db.cleanup(time.time() - KEEP_LOG_DAYS * 86400)
                await db.set("last_backup_day", today)
                log.info("Бэкап базы сделан")
            if local.hour >= 21 and await db.get("last_report_day") != today:
                stats = await db.stats_since(day_start(time.time(), cfg.tz))
                ok = sum(stats.get(s, 0) for s in ("queued", "sent", "delivered", "read"))
                bad = sum(stats.get(s, 0) for s in ("failed", "error", "notInGroup", "not_sent"))
                await notifier.owner(
                    f"📈 Итог дня: отправлено {ok}, ошибок {bad}, не проверено {stats.get('unknown', 0)}."
                )
                await db.set("last_report_day", today)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Сбой ежедневного обслуживания")


async def wait_for_telegram(bot: Bot) -> None:
    """После перезагрузки Windows сеть может подняться не сразу: ждём Telegram, а не падаем."""
    delay = 5
    while True:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            raise
        except Exception as exc:
            log.warning("Нет связи с Telegram: %s. Повтор через %s с", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            log.info("Telegram-бот @%s на связи", me.username)
            return


async def run(cfg: Config, log_handler: Handler | None = None) -> None:
    db = await Database.open(cfg.db_path)
    api = GreenAPI(cfg.green_api_url, cfg.green_media_url, cfg.green_id_instance, cfg.green_api_token)
    bot = Bot(cfg.tg_token)
    notifier = Notifier(bot, cfg.owner_id, cfg.admin_ids)
    runtime = Runtime()
    engine = Engine(db, api, notifier, runtime, cfg.tz)
    watchdog = Watchdog(api, db, engine, notifier, runtime)

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(Deps(db, api, engine, cfg, runtime, log_handler)))

    tasks: list[asyncio.Task] = []
    try:
        try:
            if await ensure_instance_settings(api):  # уведомление уйдёт, когда появится связь с Telegram
                await notifier.owner(
                    "⚙️ Настройки инстанса GREEN-API обновлены. Инстанс перезапустится, это до 5 минут."
                )
        except GreenAPIError as exc:
            log.warning("Не удалось проверить настройки инстанса: %s", exc)

        jobs = {
            "engine": engine.run(),
            "state": watchdog.run_state(),
            "notifications": watchdog.run_notifications(),
            "maintenance": maintenance(db, notifier, cfg),
        }
        tasks = [asyncio.create_task(coro, name=name) for name, coro in jobs.items()]
        await wait_for_telegram(bot)
        await notifier.owner("🤖 Сервис запущен. /start — меню.")
        log.info("Сервис запущен")
        await dispatcher.start_polling(bot)
    finally:
        log.info("Остановка сервиса")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.shutdown()
        await bot.session.close()
        await api.close()
        await db.close()
