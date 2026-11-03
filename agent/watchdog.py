"""Сторож ассистента (раздел 13 ТЗ): MT5 закрыт, робот молчит, диск. Тревоги идут через Notifier.

 - MT5 (terminal64.exe) нет дольше 30 с -> critical + автоперезапуск (не чаще 1 раза в 10 минут и 3 в час);
 - MT5 работает, а status.json старше 60 с -> critical «MT5 завис или робот снят с графика»;
 - диск меньше 1 ГБ -> warning раз в сутки;
 - выходные (рынок закрыт): тревог «нет тиков» и «нет связи» не шлём, их присылает сам робот только в сессию.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from datetime import datetime
from typing import Any, Callable

from agent.config import Settings
from agent.util import local_day, utcnow

log = logging.getLogger("watchdog")

MT5_GONE_SEC = 30
STATUS_STALE_SEC = 60
RESTART_MIN_GAP = 600
RESTART_PER_HOUR = 3
DISK_MIN_GB = 1.0


def mt5_running() -> bool:
    import psutil
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == "terminal64.exe":
            return True
    return False


def start_mt5(path: str) -> None:
    subprocess.Popen([path], close_fds=True)


class Watchdog:
    def __init__(self, settings: Settings, hub: Any, notifier: Any, db: Any,
                 is_running: Callable[[], bool] = mt5_running, restart: Callable[[str], None] = start_mt5,
                 free_gb: Callable[[], float] | None = None, clock: Callable[[], datetime] = utcnow):
        self.s, self.hub, self.n, self.db = settings, hub, notifier, db
        self.is_running, self.restart, self.clock = is_running, restart, clock
        self.free_gb = free_gb or (lambda: shutil.disk_usage(self.s.data_dir).free / 1e9)
        self._gone_since: datetime | None = None
        self._restarts: list[float] = []

    async def run(self, every: float = 5.0) -> None:
        while True:
            try:
                await self.check_once()
                await self.n.flush_backlog()
            except Exception:
                log.exception("ошибка сторожа")
            await asyncio.sleep(every)

    async def check_once(self) -> None:
        now = self.clock()
        await self._check_mt5(now)
        await self._check_status(now)
        await self._check_disk(now)

    # ------------------------------------------------------------ MT5
    async def _check_mt5(self, now: datetime) -> None:
        if self.is_running():
            if self._gone_since is not None:
                self._gone_since = None
                if self.n.is_open("mt5_down"):
                    await self.n.raise_alert("mt5_down", "closed", "info", "MT5 снова запущен")
            return
        if self._gone_since is None:
            self._gone_since = now
            return
        if (now - self._gone_since).total_seconds() < MT5_GONE_SEC:
            return
        if not self.n.is_open("mt5_down"):
            await self.n.raise_alert("mt5_down", "open", "critical",
                                     f"Терминал MT5 закрыт с {self._gone_since.astimezone(self.s.zone):%H:%M}. "
                                     "Позиции у брокера со стопами, но робот не работает.")
        if self.s.mt5_autorestart:
            await self._try_restart(now)

    async def _try_restart(self, now: datetime) -> None:
        t = now.timestamp()
        recent = [x for x in self._restarts if t - x < 3600]
        if recent and t - recent[-1] < RESTART_MIN_GAP:
            return
        if len(recent) >= RESTART_PER_HOUR:
            return
        self._restarts = recent + [t]
        try:
            self.restart(self.s.mt5_path)
            await self.n.m.text("🔁 Перезапустил MT5. Проверь, что робот встал на график и алго-торговля зелёная.")
        except Exception as e:
            await self.n.m.text(f"✖️ Перезапустить MT5 не получилось: {type(e).__name__}. Запусти вручную.")

    # ------------------------------------------------------------ молчание робота
    async def _check_status(self, now: datetime) -> None:
        if not self.is_running():
            return                                  # причина уже названа: MT5 закрыт
        st = self.hub.status()
        silent = st is None or st.age_sec > STATUS_STALE_SEC
        if silent:
            if not self.n.is_open("robot_silent") and self.hub:
                await self.n.raise_alert("robot_silent", "open", "critical",
                                         "MT5 завис или робот снят с графика: status.json не обновляется.")
        elif self.n.is_open("robot_silent"):
            await self.n.raise_alert("robot_silent", "closed", "info", "робот снова на связи")

    # ------------------------------------------------------------ диск
    async def _check_disk(self, now: datetime) -> None:
        free = self.free_gb()
        if free >= DISK_MIN_GB:
            return
        key = f"disk.notified.{local_day(now, self.s.zone)}"
        if await self.db.kv_get(key):
            return
        await self.db.kv_set(key, True)
        await self.n.m.text(f"⚠️ На диске осталось {free:.2f} ГБ. Освободи место, иначе ассистент встанет.")
