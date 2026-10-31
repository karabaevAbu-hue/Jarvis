"""SQLite: курсор событий, ожидающие «да», тревоги, журнал действий, расход API, память диалога."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_action (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, args TEXT NOT NULL, prompt TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, args TEXT NOT NULL,
    who TEXT NOT NULL, confirmed_text TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY, severity TEXT NOT NULL, opened REAL NOT NULL, closed REAL,
    text TEXT, acked INTEGER NOT NULL DEFAULT 0, call_attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, cache_write INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_usage_day ON api_usage(day);
CREATE TABLE IF NOT EXISTS trade_msg (ticket INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_memory_day ON memory(day);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> "Database":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "База не открыта"
        return self._db

    # ---------- kv ----------
    async def kv_get(self, key: str, default: Any = None) -> Any:
        async with self.db.execute("SELECT v FROM kv WHERE k=?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["v"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (key, json.dumps(value, ensure_ascii=False)))
        await self.db.commit()

    # ---------- расход API ----------
    async def add_usage(self, day: str, model: str, tin: int, tout: int, cw: int, cr: int, cost: float) -> None:
        await self.db.execute(
            "INSERT INTO api_usage(ts,day,model,tokens_in,tokens_out,cache_write,cache_read,cost) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), day, model, tin, tout, cw, cr, cost))
        await self.db.commit()

    async def usage_cost(self, day: str) -> float:
        async with self.db.execute("SELECT COALESCE(SUM(cost),0) AS c FROM api_usage WHERE day=?", (day,)) as cur:
            row = await cur.fetchone()
        return float(row["c"])

    async def usage_summary(self, day: str) -> dict:
        async with self.db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) AS tin, COALESCE(SUM(tokens_out),0) AS tout, "
                "COALESCE(SUM(cost),0) AS cost FROM api_usage WHERE day=?", (day,)) as cur:
            row = await cur.fetchone()
        return dict(row)

    # ---------- ожидающие подтверждения ----------
    async def pending_put(self, pid: str, kind: str, args: dict, prompt: str, created: float, expires: float) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO pending_action(id,kind,args,prompt,created,expires,status) VALUES(?,?,?,?,?,?,'pending')",
            (pid, kind, json.dumps(args, ensure_ascii=False), prompt, created, expires))
        await self.db.commit()

    async def pending_set_status(self, pid: str, status: str, only_if: str | None = None) -> bool:
        """Меняет статус; с only_if — только если сейчас именно он (одноразовость). Возвращает, получилось ли."""
        if only_if:
            cur = await self.db.execute("UPDATE pending_action SET status=? WHERE id=? AND status=?", (status, pid, only_if))
        else:
            cur = await self.db.execute("UPDATE pending_action SET status=? WHERE id=?", (status, pid))
        await self.db.commit()
        return cur.rowcount > 0

    # ---------- журнал действий ----------
    async def log_action(self, kind: str, args: dict, who: str, confirmed_text: str | None, result: str | None) -> None:
        await self.db.execute(
            "INSERT INTO action_log(ts,kind,args,who,confirmed_text,result) VALUES(?,?,?,?,?,?)",
            (time.time(), kind, json.dumps(args, ensure_ascii=False), who, confirmed_text, result))
        await self.db.commit()

    async def recent_actions(self, since_ts: float) -> list[dict]:
        async with self.db.execute("SELECT * FROM action_log WHERE ts>=? ORDER BY ts", (since_ts,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---------- связка тикет -> сообщение ----------
    async def set_trade_msg(self, ticket: int, message_id: int) -> None:
        await self.db.execute("INSERT OR REPLACE INTO trade_msg(ticket,message_id) VALUES(?,?)", (ticket, message_id))
        await self.db.commit()

    async def get_trade_msg(self, ticket: int) -> int | None:
        async with self.db.execute("SELECT message_id FROM trade_msg WHERE ticket=?", (ticket,)) as cur:
            row = await cur.fetchone()
        return int(row["message_id"]) if row else None

    # ---------- память диалога ----------
    async def mem_add(self, day: str, role: str, content: str) -> None:
        await self.db.execute("INSERT INTO memory(day,role,content,ts) VALUES(?,?,?,?)", (day, role, content, time.time()))
        await self.db.commit()

    async def mem_recent(self, day: str, limit: int) -> list[dict]:
        async with self.db.execute(
                "SELECT role, content FROM (SELECT * FROM memory WHERE day=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (day, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def mem_purge_before(self, day: str) -> None:
        await self.db.execute("DELETE FROM memory WHERE day<?", (day,))
        await self.db.commit()
