"""Мост со стороны ассистента: статус, события, команды, heartbeat, уборка.

BridgeHub опрашивает папку робота и раздаёт события подписчикам. Команда роботу — это файл
cmd/<id>.json; ответ приходит событием cmd_result с тем же id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

from agent.bridge.events import Event, EventReader
from agent.bridge.protocol import COMMAND_TTL_SEC, BridgePaths, Status, read_status, validate_command
from agent.storage.db import Database
from agent.util import iso_z, utcnow, write_atomic

log = logging.getLogger("bridge.hub")

Subscriber = Callable[[Event], Awaitable[None] | None]

EVENTS_KEEP_DAYS = 30
SHOTS_KEEP_DAYS = 7


@dataclass
class CmdResult:
    ok: bool
    msg: str
    id: str = ""
    data: dict | None = None
    timeout: bool = False


class BridgeHub:
    def __init__(self, paths: BridgePaths, db: Database, bridge_key: str, poll_sec: float = 1.0):
        self.paths = paths
        self.db = db
        self.key = bridge_key
        self.poll_sec = poll_sec
        self.reader = EventReader(paths, db)
        self._subs: list[Subscriber] = []
        self._waiters: dict[str, asyncio.Future[CmdResult]] = {}
        self._task: asyncio.Task | None = None

    # ---------- статус ----------
    def status(self) -> Status | None:
        return read_status(self.paths)

    # ---------- подписки ----------
    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    async def poll_once(self) -> list[Event]:
        events = await self.reader.poll()
        for ev in events:
            if ev.type == "cmd_result":
                fut = self._waiters.pop(str(ev.data.get("id", "")), None)
                if fut and not fut.done():
                    fut.set_result(CmdResult(bool(ev.data.get("ok")), str(ev.data.get("msg", "")),
                                             id=str(ev.data.get("id", "")), data=ev.data))
            for fn in self._subs:
                try:
                    r = fn(ev)
                    if asyncio.iscoroutine(r):
                        await r
                except Exception:  # один сбойный подписчик не должен ронять мост
                    log.exception("подписчик упал на событии %s", ev.type)
        return events

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("ошибка опроса моста")
            await asyncio.sleep(self.poll_sec)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ---------- команды ----------
    def write_command(self, cmd: str, args: dict[str, Any] | None = None) -> str:
        """Пишет cmd/<id>.tmp и переименовывает в .json. Возвращает id."""
        ok, msg = validate_command(cmd, args)
        if not ok:
            raise ValueError(msg)
        cid = uuid.uuid4().hex
        now = utcnow()
        body = {"id": cid, "cmd": cmd, "args": args or {}, "ts_utc": iso_z(now),
                "expires_utc": iso_z(now + timedelta(seconds=COMMAND_TTL_SEC)), "key": self.key}
        self.paths.cmd_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.cmd_dir / f"{cid}.tmp"
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.paths.cmd_dir / f"{cid}.json")
        return cid

    async def command(self, cmd: str, args: dict[str, Any] | None = None, timeout: float = 10.0) -> CmdResult:
        """Отправляет команду и ждёт cmd_result (если хаб запущен) или опрашивает сам."""
        try:
            cid = self.write_command(cmd, args)
        except ValueError as e:
            return CmdResult(False, str(e))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[CmdResult] = loop.create_future()
        self._waiters[cid] = fut
        deadline = loop.time() + timeout
        try:
            while not fut.done():
                if self._task is None:  # хаб не запущен фоном: опрашиваем сами
                    await self.poll_once()
                if fut.done():
                    break
                left = deadline - loop.time()
                if left <= 0:
                    self._waiters.pop(cid, None)
                    return CmdResult(False, f"робот не ответил за {timeout:g} с", id=cid, timeout=True)
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=min(0.2, left))
                except asyncio.TimeoutError:
                    pass
            return fut.result()
        finally:
            self._waiters.pop(cid, None)

    # ---------- heartbeat ----------
    def write_heartbeat(self) -> None:
        write_atomic(self.paths.heartbeat, json.dumps({"ts_utc": iso_z(utcnow()), "pid": os.getpid()}))

    async def heartbeat_loop(self, every: float = 10.0) -> None:
        while True:
            try:
                self.write_heartbeat()
            except OSError:
                log.exception("heartbeat не записан")
            await asyncio.sleep(every)

    # ---------- уборка ----------
    def cleanup(self) -> dict[str, int]:
        """События старше 30 дней и скрины старше 7 дней (раздел 6)."""
        now = utcnow().timestamp()
        removed = {"events": 0, "shots": 0}
        for d, days, key, pat in ((self.paths.events_dir, EVENTS_KEEP_DAYS, "events", "*.jsonl"),
                                  (self.paths.shots_dir, SHOTS_KEEP_DAYS, "shots", "*.png")):
            if not d.is_dir():
                continue
            for p in d.glob(pat):
                if now - p.stat().st_mtime > days * 86400:
                    p.unlink(missing_ok=True)
                    removed[key] += 1
        return removed


class LazyHub:
    """Мост, который появляется, когда найдена папка робота. До этого команды честно отвечают «робот не найден»."""

    def __init__(self) -> None:
        self.inner: BridgeHub | None = None

    def attach(self, hub: BridgeHub) -> None:
        self.inner = hub

    def __bool__(self) -> bool:
        return self.inner is not None

    @property
    def paths(self):
        return self.inner.paths if self.inner else None

    def status(self):
        return self.inner.status() if self.inner else None

    async def command(self, cmd, args=None, timeout: float = 10.0) -> CmdResult:
        if not self.inner:
            return CmdResult(False, "робот не найден: нет папки моста")
        return await self.inner.command(cmd, args, timeout)
