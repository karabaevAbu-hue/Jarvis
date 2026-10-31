"""Чтение ленты событий робота (events/YYYYMMDD.jsonl) с курсором в базе.

Правила раздела 6 ТЗ:
 - курсор = (файл, смещение в байтах, последний seq), переживает перезапуск;
 - при первом запуске начинаем с конца текущего файла;
 - неполную последнюю строку (без \\n) не читаем;
 - пропуск seq пишем в лог.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent.bridge.protocol import BridgePaths
from agent.storage.db import Database

log = logging.getLogger("bridge.events")


@dataclass
class Event:
    seq: int
    type: str
    level: int = 1
    severity: str = "info"
    ts_utc: str = ""
    ts_local: str = ""
    robot: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Event":
        return cls(seq=int(d.get("seq", 0)), type=str(d.get("type", "")), level=int(d.get("level", 1)),
                   severity=str(d.get("severity", "info")), ts_utc=str(d.get("ts_utc", "")),
                   ts_local=str(d.get("ts_local", "")), robot=str(d.get("robot", "")),
                   data=d.get("data") or {}, text=str(d.get("text", "")))


class EventReader:
    def __init__(self, paths: BridgePaths, db: Database):
        self.paths = paths
        self.db = db
        self.key = f"bridge.cursor.{paths.robot}"
        self.gaps: list[tuple[int, int]] = []  # (ожидали, получили)

    def _files(self) -> list[str]:
        d = self.paths.events_dir
        return sorted(p.name for p in d.glob("*.jsonl")) if d.is_dir() else []

    async def poll(self) -> list[Event]:
        files = self._files()
        cursor = await self.db.kv_get(self.key)
        if cursor is None:
            if not files:
                # робот ещё не писал: когда файл появится, читаем его с начала
                cursor = {"file": "", "offset": 0, "seq": None}
            else:
                last = files[-1]
                cursor = {"file": last, "offset": (self.paths.events_dir / last).stat().st_size, "seq": None}
            await self.db.kv_set(self.key, cursor)
            if cursor["file"]:
                return []

        out: list[Event] = []
        changed = False
        # не больше нескольких файлов за цикл, чтобы не зациклиться
        for _ in range(5):
            path = (self.paths.events_dir / cursor["file"]) if cursor["file"] else None
            progressed = False
            if path is not None and path.exists():
                with open(path, "rb") as f:
                    f.seek(cursor["offset"])
                    chunk = f.read()
                cut = chunk.rfind(b"\n")
                if cut >= 0:
                    complete = chunk[: cut + 1]
                    cursor["offset"] += len(complete)
                    changed = progressed = True
                    for raw in complete.split(b"\n"):
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            ev = Event.from_json(json.loads(raw.decode("utf-8")))
                        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                            log.warning("битая строка в %s пропущена", cursor["file"])
                            continue
                        last_seq = cursor["seq"]
                        if last_seq is not None and ev.seq <= last_seq:
                            continue  # уже видели (перезапуск, дубль)
                        if last_seq is not None and ev.seq != last_seq + 1:
                            log.warning("пропуск seq: ожидали %s, пришёл %s", last_seq + 1, ev.seq)
                            self.gaps.append((last_seq + 1, ev.seq))
                        cursor["seq"] = ev.seq
                        out.append(ev)
            # текущий файл дочитан: если есть более новый, переходим на него
            newer = [n for n in self._files() if n > cursor["file"]]
            if newer and (path is None or not path.exists() or not self._has_partial(path, cursor["offset"])):
                cursor = {"file": newer[0], "offset": 0, "seq": cursor["seq"]}
                changed = True
                continue
            if not progressed:
                break
        if changed:
            await self.db.kv_set(self.key, cursor)
        return out

    @staticmethod
    def _has_partial(path, offset: int) -> bool:
        return path.stat().st_size > offset
