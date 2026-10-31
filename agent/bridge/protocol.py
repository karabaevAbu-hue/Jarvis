"""Контракт моста (раздел 6 и 8 ТЗ): пути, проверка команд, разбор статуса.

Один и тот же `validate_command` использует ассистент (до отправки) и симулятор робота
(как робот проверяет у себя).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.util import parse_iso, utcnow

COMMAND_TTL_SEC = 60
TF_CHOICES = ("M5", "M15", "H1", "H4")

# диапазоны из раздела 8
SET_RANGES = {
    "risk_pct": (0.1, 5.0),
    "daily_risk_pct": (1.0, 20.0),
    "max_trades": (1, 10),
    "rr": (1.0, 5.0),
}
SET_BOOLS = ("weak", "buy", "sell")
OPEN_SL_MAX_PTS = 50.0

NO_ARGS_CMDS = ("pause", "resume", "close", "be", "reset", "ping", "status")
ALL_CMDS = NO_ARGS_CMDS + ("modify", "set", "open", "chart")


@dataclass
class BridgePaths:
    """Папка одного робота: <root>/<login>_<magic>/"""
    root: Path
    robot: str

    @property
    def base(self) -> Path:
        return self.root / self.robot

    @property
    def status(self) -> Path:
        return self.base / "status.json"

    @property
    def events_dir(self) -> Path:
        return self.base / "events"

    @property
    def cmd_dir(self) -> Path:
        return self.base / "cmd"

    @property
    def settings(self) -> Path:
        return self.base / "settings.json"

    @property
    def trades_csv(self) -> Path:
        return self.base / "trades.csv"

    @property
    def shots_dir(self) -> Path:
        return self.base / "shots"

    @property
    def heartbeat(self) -> Path:
        return self.base / "bot_heartbeat.json"

    def ensure(self) -> None:
        for d in (self.base, self.events_dir, self.cmd_dir, self.shots_dir):
            d.mkdir(parents=True, exist_ok=True)


def discover_robots(root: Path) -> list[str]:
    """Папки вида <login>_<magic> в корне моста."""
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and "_" in p.name and p.name.split("_")[0].isdigit():
            out.append(p.name)
    return out


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_command(cmd: str, args: dict[str, Any] | None) -> tuple[bool, str]:
    """Проверка команды и диапазонов. Возвращает (ok, сообщение по-русски)."""
    args = args or {}
    if cmd not in ALL_CMDS:
        return False, f"неизвестная команда: {cmd}"
    if cmd in NO_ARGS_CMDS:
        return True, "ok"
    if cmd == "chart":
        tf = args.get("tf", "M5")
        if tf not in TF_CHOICES:
            return False, f"таймфрейм {tf} не поддерживается (M5, M15, H1, H4)"
        return True, "ok"
    if cmd == "modify":
        keys = [k for k in ("sl", "tp") if k in args]
        if not keys:
            return False, "нужен новый sl или tp"
        for k in keys:
            if not _is_num(args[k]) or args[k] <= 0:
                return False, f"{k}: нужна цена больше нуля"
        return True, "ok"
    if cmd == "set":
        known = set(SET_RANGES) | set(SET_BOOLS)
        unknown = [k for k in args if k not in known]
        if unknown:
            return False, f"неизвестная настройка: {unknown[0]}"
        if not args:
            return False, "не указана настройка"
        for k, (lo, hi) in SET_RANGES.items():
            if k in args:
                v = args[k]
                if not _is_num(v) or not (lo <= v <= hi):
                    return False, f"{k}: допустимо от {lo} до {hi}"
                if k == "max_trades" and int(v) != v:
                    return False, "max_trades: нужно целое число"
        for k in SET_BOOLS:
            if k in args and not isinstance(args[k], bool):
                return False, f"{k}: нужно true или false"
        return True, "ok"
    if cmd == "open":
        if args.get("side") not in ("BUY", "SELL"):
            return False, "side: BUY или SELL"
        sl = args.get("sl_pts")
        if not _is_num(sl) or not (0 < sl <= OPEN_SL_MAX_PTS):
            return False, f"sl_pts: стоп обязателен, от 0 до {OPEN_SL_MAX_PTS:g} пунктов"
        if "risk_pct" in args:
            lo, hi = SET_RANGES["risk_pct"]
            if not _is_num(args["risk_pct"]) or not (lo <= args["risk_pct"] <= hi):
                return False, f"risk_pct: допустимо от {lo} до {hi}"
        if "rr" in args:
            lo, hi = SET_RANGES["rr"]
            if not _is_num(args["rr"]) or not (lo <= args["rr"] <= hi):
                return False, f"rr: допустимо от {lo} до {hi}"
        return True, "ok"
    return False, "не проверено"


@dataclass
class Status:
    """Обёртка над status.json с возрастом и удобными полями."""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ts(self):
        s = self.raw.get("ts_utc")
        return parse_iso(s) if s else None

    @property
    def age_sec(self) -> float:
        ts = self.ts
        return float("inf") if ts is None else (utcnow() - ts).total_seconds()

    @property
    def position(self) -> dict | None:
        return self.raw.get("position")

    @property
    def paused(self) -> bool:
        return bool(self.raw.get("paused"))

    @property
    def market_open(self) -> bool:
        return bool(self.raw.get("symbol", {}).get("market_open", True))

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


def read_status(paths: BridgePaths) -> Status | None:
    try:
        text = paths.status.read_text(encoding="utf-8")
        return Status(json.loads(text))
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return None
