"""Короткая память диалога: последние 20 сообщений + выжимка дня; новый день — чистая история."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from agent.bridge.protocol import Status
from agent.config import Settings
from agent.storage.db import Database
from agent.util import local_day, utcnow

HISTORY = 20


class Memory:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow):
        self.db, self.s, self.clock = db, settings, clock

    def day(self) -> str:
        return local_day(self.clock(), self.s.zone)

    async def add(self, role: str, content: str) -> None:
        if content.strip():
            await self.db.mem_add(self.day(), role, content)

    async def history(self, limit: int = HISTORY) -> list[dict]:
        """Сообщения за сегодня в формате API: начинается с user, соседние одной роли склеены."""
        rows = await self.db.mem_recent(self.day(), limit)
        out: list[dict] = []
        for r in rows:
            if not out and r["role"] != "user":
                continue
            if out and out[-1]["role"] == r["role"]:
                out[-1]["content"] += "\n" + r["content"]
            else:
                out.append({"role": r["role"], "content": r["content"]})
        return out

    async def new_day_cleanup(self) -> None:
        await self.db.mem_purge_before(self.day())

    async def digest(self, st: Status | None) -> str:
        """Выжимка дня без ИИ: что сделано по журналу действий и что на счёте."""
        import time
        from datetime import timedelta
        since = (self.clock().astimezone(self.s.zone).replace(hour=0, minute=0, second=0, microsecond=0)).timestamp()
        acts = await self.db.recent_actions(since)
        done = [a for a in acts if str(a.get("result", "")).startswith("ok")]
        parts = [f"Подтверждённых действий сегодня: {len(done)}."] if acts else []
        if st:
            d = st.get("day", default={}) or {}
            parts.append(f"День: {d.get('trades', 0)} сделок, {d.get('usd', 0):+.2f} USD.")
        return " ".join(parts)
