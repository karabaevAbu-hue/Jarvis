"""SQLite: заказы дня, группы, настройки, журнал отправок."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    slot        INTEGER PRIMARY KEY CHECK (slot BETWEEN 1 AND 3),
    text        TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL,
    updated_by  INTEGER
);
CREATE TABLE IF NOT EXISTS groups (
    chat_id      TEXT PRIMARY KEY,
    name         TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT 'live',   -- live | quiet
    interval_min INTEGER,                           -- свой интервал, иначе по типу
    enabled      INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'ok',     -- ok | removed | error
    fail_streak  INTEGER NOT NULL DEFAULT 0,
    next_send_at REAL,
    last_header  INTEGER,
    present      INTEGER NOT NULL DEFAULT 1,
    synced_at    REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    chat_id     TEXT NOT NULL,
    slot_time   REAL,
    id_message  TEXT,
    status      TEXT NOT NULL,   -- sending | queued | sent | delivered | read | failed | notInGroup | unknown | not_sent | error
    error       TEXT,
    text        TEXT,
    kind        TEXT NOT NULL DEFAULT 'auto'   -- auto | test
);
CREATE INDEX IF NOT EXISTS ix_send_log_msg ON send_log (id_message);
CREATE INDEX IF NOT EXISTS ix_send_log_created ON send_log (created_at);
"""

DEFAULT_SETTINGS = {
    "running": "0",
    "live_interval": "15",
    "quiet_interval": "45",
    "pause_min": "20",
    "pause_max": "30",
    "quiet_start": "23:00",
    "quiet_end": "07:00",
    "daily_limit": "200",
    "max_chars": "1200",
    "header_1": "🚚 Актуальные заказы",
    "header_2": "📦 Грузы на сегодня",
    "header_3": "🚛 Ищем транспорт",
    "footer": "",
    "last_backup_day": "",
    "last_report_day": "",
}

# Статусы, которые считаются отправкой при подсчёте дневного лимита
_COUNTED = ("sending", "queued", "sent", "delivered", "read", "unknown")


class Database:
    def __init__(self, conn: aiosqlite.Connection, path: Path, clock=time.time):
        self._conn = conn
        self.path = path
        self._clock = clock

    @classmethod
    async def open(cls, path: Path, clock=time.time) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        await conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", list(DEFAULT_SETTINGS.items())
        )
        await conn.commit()
        return cls(conn, path, clock)

    async def close(self) -> None:
        await self._conn.close()

    # --- настройки ---
    async def get(self, key: str) -> str:
        async with self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    async def get_int(self, key: str) -> int:
        try:
            return int(await self.get(key))
        except ValueError:
            return int(DEFAULT_SETTINGS[key])

    async def set(self, key: str, value: str | int) -> None:
        await self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await self._conn.commit()

    # --- заказы дня ---
    async def list_orders(self) -> list[aiosqlite.Row]:
        async with self._conn.execute("SELECT * FROM orders ORDER BY slot") as cur:
            return list(await cur.fetchall())

    async def active_orders(self) -> list[aiosqlite.Row]:
        async with self._conn.execute("SELECT * FROM orders WHERE active = 1 ORDER BY slot") as cur:
            return list(await cur.fetchall())

    async def set_order(self, slot: int, text: str, user_id: int | None) -> None:
        await self._conn.execute(
            "INSERT INTO orders (slot, text, active, version, updated_at, updated_by) VALUES (?, ?, 1, 1, ?, ?) "
            "ON CONFLICT(slot) DO UPDATE SET text = excluded.text, active = 1, version = orders.version + 1, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (slot, text, self._clock(), user_id),
        )
        await self._conn.commit()

    async def remove_order(self, slot: int) -> None:
        await self._conn.execute("UPDATE orders SET active = 0, updated_at = ? WHERE slot = ?", (self._clock(), slot))
        await self._conn.commit()

    # --- группы ---
    async def sync_groups(self, groups: list[tuple[str, str]]) -> tuple[int, int]:
        """Сверяет группы из WhatsApp с базой. Возвращает (новых, пропавших)."""
        now = self._clock()
        async with self._conn.execute("SELECT chat_id, present FROM groups") as cur:
            known = {r["chat_id"]: r["present"] for r in await cur.fetchall()}
        seen = {chat_id for chat_id, _ in groups}
        new = 0
        for chat_id, name in groups:
            if chat_id not in known:
                new += 1
            await self._conn.execute(
                "INSERT INTO groups (chat_id, name, present, synced_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name, present = 1, synced_at = excluded.synced_at, "
                "status = CASE WHEN groups.status = 'removed' THEN 'ok' ELSE groups.status END",
                (chat_id, name or chat_id, now),
            )
        gone = [c for c, present in known.items() if present and c not in seen]
        for chat_id in gone:
            await self._conn.execute(
                "UPDATE groups SET present = 0, status = 'removed', next_send_at = NULL WHERE chat_id = ?", (chat_id,)
            )
        await self._conn.commit()
        return new, len(gone)

    async def list_groups(self) -> list[aiosqlite.Row]:
        async with self._conn.execute("SELECT * FROM groups ORDER BY enabled DESC, name COLLATE NOCASE") as cur:
            return list(await cur.fetchall())

    async def get_group(self, chat_id: str) -> aiosqlite.Row | None:
        async with self._conn.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,)) as cur:
            return await cur.fetchone()

    async def enabled_groups(self) -> list[aiosqlite.Row]:
        async with self._conn.execute(
            "SELECT * FROM groups WHERE enabled = 1 AND status = 'ok' AND present = 1 ORDER BY name COLLATE NOCASE"
        ) as cur:
            return list(await cur.fetchall())

    async def enabled_count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) AS n FROM groups WHERE enabled = 1") as cur:
            return (await cur.fetchone())["n"]

    async def set_group_enabled(self, chat_id: str, enabled: bool, next_send_at: float | None = None) -> None:
        await self._conn.execute(
            "UPDATE groups SET enabled = ?, next_send_at = ?, fail_streak = 0, "
            "status = CASE WHEN status = 'error' THEN 'ok' ELSE status END WHERE chat_id = ?",
            (1 if enabled else 0, next_send_at if enabled else None, chat_id),
        )
        await self._conn.commit()

    async def set_group_kind(self, chat_id: str, kind: str) -> None:
        await self._conn.execute("UPDATE groups SET kind = ? WHERE chat_id = ?", (kind, chat_id))
        await self._conn.commit()

    async def set_group_status(self, chat_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE groups SET status = ?, next_send_at = CASE WHEN ? = 'ok' THEN next_send_at ELSE NULL END "
            "WHERE chat_id = ?",
            (status, status, chat_id),
        )
        await self._conn.commit()

    async def inc_fail(self, chat_id: str) -> int:
        await self._conn.execute("UPDATE groups SET fail_streak = fail_streak + 1 WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()
        group = await self.get_group(chat_id)
        return group["fail_streak"] if group else 0

    async def reset_fail(self, chat_id: str) -> None:
        await self._conn.execute("UPDATE groups SET fail_streak = 0 WHERE chat_id = ?", (chat_id,))
        await self._conn.commit()

    # --- расписание ---
    async def due_group(self, now: float) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM groups WHERE enabled = 1 AND status = 'ok' AND present = 1 "
            "AND next_send_at IS NOT NULL AND next_send_at <= ? ORDER BY next_send_at LIMIT 1",
            (now,),
        ) as cur:
            return await cur.fetchone()

    async def next_due_time(self) -> float | None:
        async with self._conn.execute(
            "SELECT MIN(next_send_at) AS t FROM groups WHERE enabled = 1 AND status = 'ok' AND present = 1"
        ) as cur:
            row = await cur.fetchone()
        return row["t"] if row else None

    async def set_next_send(self, chat_id: str, ts: float | None, last_header: int | None = None) -> None:
        if last_header is None:
            await self._conn.execute("UPDATE groups SET next_send_at = ? WHERE chat_id = ?", (ts, chat_id))
        else:
            await self._conn.execute(
                "UPDATE groups SET next_send_at = ?, last_header = ? WHERE chat_id = ?", (ts, last_header, chat_id)
            )
        await self._conn.commit()

    async def clear_schedule(self) -> None:
        await self._conn.execute("UPDATE groups SET next_send_at = NULL")
        await self._conn.commit()

    # --- журнал ---
    async def log_sending(self, chat_id: str, slot_time: float | None, text: str, kind: str = "auto") -> int:
        cur = await self._conn.execute(
            "INSERT INTO send_log (created_at, chat_id, slot_time, status, text, kind) VALUES (?, ?, ?, 'sending', ?, ?)",
            (self._clock(), chat_id, slot_time, text, kind),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def log_update(self, log_id: int, status: str, *, id_message: str | None = None, error: str | None = None):
        await self._conn.execute(
            "UPDATE send_log SET status = ?, id_message = COALESCE(?, id_message), error = COALESCE(?, error) "
            "WHERE id = ?",
            (status, id_message, error, log_id),
        )
        await self._conn.commit()

    async def log_status_by_message(self, id_message: str, status: str) -> str | None:
        """Обновляет статус по idMessage из уведомления. Возвращает chat_id или None."""
        async with self._conn.execute("SELECT id, chat_id FROM send_log WHERE id_message = ?", (id_message,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await self._conn.execute("UPDATE send_log SET status = ? WHERE id = ?", (status, row["id"]))
        await self._conn.commit()
        return row["chat_id"]

    async def mark_stale_sending(self) -> int:
        """После перезапуска: записи «отправляется» без ответа помечаются unknown. Повторно не шлём."""
        cur = await self._conn.execute("UPDATE send_log SET status = 'unknown' WHERE status = 'sending'")
        await self._conn.commit()
        return cur.rowcount

    async def sent_since(self, ts: float) -> int:
        marks = ",".join("?" * len(_COUNTED))
        async with self._conn.execute(
            f"SELECT COUNT(*) AS n FROM send_log WHERE created_at >= ? AND status IN ({marks})", (ts, *_COUNTED)
        ) as cur:
            return (await cur.fetchone())["n"]

    async def stats_since(self, ts: float) -> dict[str, int]:
        async with self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM send_log WHERE created_at >= ? GROUP BY status", (ts,)
        ) as cur:
            return {r["status"]: r["n"] for r in await cur.fetchall()}

    async def cleanup(self, older_than: float) -> None:
        await self._conn.execute("DELETE FROM send_log WHERE created_at < ?", (older_than,))
        await self._conn.commit()

    async def backup_to(self, dest: Path) -> None:
        """Горячий бэкап через sqlite backup API (копировать файл нельзя: свежие данные лежат в -wal)."""

        def _backup() -> None:
            src = sqlite3.connect(self.path)
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

        await asyncio.to_thread(_backup)
