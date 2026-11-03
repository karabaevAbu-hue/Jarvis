"""Расписание: утренний план, зоны на завтра, итог дня, недельный разбор, уборка (раздел 13 ТЗ).

Каждое задание срабатывает раз в сутки после своего времени (Бишкек). Если ассистент был выключен и
поднялся позже, задание выполняется в пределах окна `grace_min`, дальше пропускается (устаревший план хуже тишины).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Awaitable, Callable

from agent.config import Settings, parse_hm
from agent.storage.db import Database
from agent.util import local_day, utcnow

log = logging.getLogger("scheduler")

WEEKDAYS = {0, 1, 2, 3, 4}
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass
class Job:
    name: str
    at: dtime
    days: set[int]
    fn: Callable[[], Awaitable[None]]
    grace_min: int = 30


def parse_weekly(spec: str) -> tuple[int, dtime]:
    """'Sat 10:00' -> (5, time(10, 0))"""
    d, hm = spec.split()
    return DAYS[d.lower()[:3]], parse_hm(hm)  # type: ignore[return-value]


class Scheduler:
    def __init__(self, settings: Settings, db: Database, clock: Callable[[], datetime] = utcnow):
        self.s, self.db, self.clock = settings, db, clock
        self.jobs: list[Job] = []

    def add(self, name: str, hm: str, days: set[int], fn: Callable[[], Awaitable[None]], grace_min: int = 30) -> None:
        at = parse_hm(hm)
        if at is None:                      # время не задано: задание выключено
            return
        self.jobs.append(Job(name, at, days, fn, grace_min))

    def add_weekly(self, name: str, spec: str, fn: Callable[[], Awaitable[None]]) -> None:
        wd, at = parse_weekly(spec)
        self.jobs.append(Job(name, at, {wd}, fn, 120))

    async def due(self, now_local: datetime) -> list[Job]:
        out = []
        for j in self.jobs:
            if now_local.weekday() not in j.days:
                continue
            start = now_local.replace(hour=j.at.hour, minute=j.at.minute, second=0, microsecond=0)
            late = (now_local - start).total_seconds() / 60
            if late < 0 or late > j.grace_min:
                continue
            if await self.db.kv_get(f"sched.{j.name}.{now_local:%Y-%m-%d}"):
                continue
            out.append(j)
        return out

    async def tick(self) -> list[str]:
        """Одна проверка. Возвращает имена выполненных заданий."""
        now = self.clock().astimezone(self.s.zone)
        ran = []
        for j in await self.due(now):
            await self.db.kv_set(f"sched.{j.name}.{now:%Y-%m-%d}", True)      # до запуска: без повторов при сбое
            try:
                await j.fn()
                ran.append(j.name)
            except Exception:
                log.exception("задание %s упало", j.name)
        return ran

    async def run(self, every: float = 20.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("ошибка планировщика")
            await asyncio.sleep(every)
