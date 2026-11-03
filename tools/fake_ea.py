"""Симулятор робота MT5 v3.3–v3.4: пишет мост так, как его должен писать настоящий робот.

Нужен, чтобы разрабатывать и тестировать ассистента на Linux без MT5, и как эталон контракта
для робота v3.3 (разделы 5-8 ТЗ). Торговой логики здесь нет: позиции и цены задаются вручную
или случайным блужданием в режиме --demo.

Запуск:  python -m tools.fake_ea --root data/bridge --key SECRET --demo
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from agent.bridge.protocol import BridgePaths, validate_command
from agent.bridge.trades import append_trade
from agent.util import UTC, iso_z, local_str, parse_iso, utcnow, write_atomic

TZ = ZoneInfo("Asia/Bishkek")
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cf"
    "c0f01f0005000201c0aa4d0b0000000049454e44ae426082")
DEFAULTS = {"paused": False, "risk_pct": 1.0, "daily_risk_pct": 5.0, "max_trades": 5, "rr": 2.0,
            "weak": True, "buy": True, "sell": True}
BOT_ALIVE_SEC = 30
POINT_USD_PER_LOT = 100.0  # золото: 1 пункт цены x 1 лот = 100 USD


class FakeEA:
    def __init__(self, paths: BridgePaths, key: str, login: int = 12345678, magic: int = 777001,
                 symbol: str = "XAUUSDm", balance: float = 1000.0,
                 now_fn: Callable[[], datetime] = utcnow):
        self.paths = paths
        self.key = key
        self.login = login
        self.symbol = symbol
        self.now = now_fn
        self.balance = balance
        self.day_start_balance = balance
        self.bid, self.ask = 2650.12, 2650.31
        self.seq = 0
        self.set = dict(DEFAULTS)
        self.position: dict[str, Any] | None = None
        self.day = {"trades": 0, "counter": 0, "pts": 0.0, "usd": 0.0, "loss_streak": 0}
        self.alerts: dict[str, dict] = {}
        self.pause_reason = ""
        self.zones: list[dict] = []
        self.bias = {"d1": "BUY", "h4": "SELL"}
        self.last_skip = ""
        self.connected = True
        self._ticket = 123456
        self._status_at = 0.0
        self.version = "3.40"

    # ---------- жизненный цикл ----------
    def start(self) -> None:
        self.paths.ensure()
        if self.paths.settings.exists():
            saved = json.loads(self.paths.settings.read_text(encoding="utf-8"))
            self.seq = int(saved.get("seq", 0))
            self.set.update({k: v for k, v in saved.get("settings", {}).items() if k in DEFAULTS})
        else:
            self._save_settings()
        self.emit("ea_started", 0, "info", {"version": self.version, "settings": dict(self.set)},
                  f"▶️ Робот запущен: v{self.version} · {self.symbol} · демо · риск {self.set['risk_pct']:g}% / "
                  f"{self.set['daily_risk_pct']:g}% в день")

    def stop(self, reason: str = "снят с графика") -> None:
        self.emit("ea_stopped", 0, "info", {"reason": reason}, f"⏹ Робот остановлен: {reason}")

    @property
    def robot_id(self) -> str:
        return self.paths.robot

    # ---------- события ----------
    def emit(self, type_: str, level: int = 1, severity: str = "info", data: dict | None = None,
             text: str = "") -> int:
        self.seq += 1
        now = self.now()
        ev = {"seq": self.seq, "ts_utc": iso_z(now), "ts_local": local_str(now, TZ), "type": type_,
              "level": level, "severity": severity, "robot": self.robot_id, "data": data or {}, "text": text}
        day = now.astimezone(TZ).strftime("%Y%m%d")
        self.paths.events_dir.mkdir(parents=True, exist_ok=True)
        with open(self.paths.events_dir / f"{day}.jsonl", "ab") as f:
            f.write((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
        self._save_settings()
        self.write_status()
        return self.seq

    def _save_settings(self) -> None:
        write_atomic(self.paths.settings, json.dumps({"seq": self.seq, "settings": self.set}, ensure_ascii=False))

    # ---------- статус ----------
    def _bot_alive(self) -> tuple[bool, str | None]:
        try:
            hb = json.loads(self.paths.heartbeat.read_text(encoding="utf-8"))
            ts = parse_iso(hb["ts_utc"])
            return (self.now() - ts).total_seconds() <= BOT_ALIVE_SEC, hb["ts_utc"]
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            return False, None

    def status_dict(self) -> dict:
        now = self.now()
        alive, hb = self._bot_alive()
        eq = self.balance + (self.position["profit"] if self.position else 0.0)
        risk_left = max(0.0, self.day_start_balance * self.set["daily_risk_pct"] / 100.0 + min(0.0, self.day["usd"]))
        return {
            "schema": 1, "robot": self.robot_id, "version": self.version, "ts_utc": iso_z(now),
            "ts_local": local_str(now, TZ),
            "terminal": {"connected": self.connected, "algo_allowed": True, "ea_trade_allowed": True, "ping_ms": 41},
            "account": {"login": self.login, "server": "Exness-MT5Trial", "currency": "USD", "mode": "demo",
                        "balance": round(self.balance, 2), "equity": round(eq, 2), "margin_free": 980.1,
                        "margin_level": 4200.0, "equity_stop_usd": 0.0},
            "symbol": {"name": self.symbol, "bid": self.bid, "ask": self.ask,
                       "spread": round(self.ask - self.bid, 2), "last_tick_utc": iso_z(now), "market_open": True},
            "session": {"open": True, "name": "LONDON", "relaxed": True},
            "paused": self.set["paused"], "pause_reason": self.pause_reason, "bias": self.bias,
            "zones": self.zones,
            "day": {**self.day, "max_trades": self.set["max_trades"], "risk_left": round(risk_left, 2), "blocked": ""},
            "position": self.position,
            "settings": {k: v for k, v in self.set.items() if k != "paused"},
            "bridge": {"bot_alive": alive, "bot_heartbeat_utc": hb},
            "last_skip": self.last_skip,
        }

    def write_status(self) -> None:
        write_atomic(self.paths.status, json.dumps(self.status_dict(), ensure_ascii=False))

    # ---------- сделки (ручное управление сценарием) ----------
    def open_position(self, side: str, sl_pts: float, entry: float | None = None, trigger: str = "A",
                      manual: bool = False, risk_pct: float | None = None, rr: float | None = None) -> dict:
        assert self.position is None, "позиция уже открыта"
        entry = entry or (self.ask if side == "BUY" else self.bid)
        risk_pct = risk_pct or self.set["risk_pct"]
        rr = rr or self.set["rr"]
        risk_usd = self.balance * risk_pct / 100.0
        lot = max(0.01, round(risk_usd / (sl_pts * POINT_USD_PER_LOT), 2))
        sign = 1 if side == "BUY" else -1
        self._ticket += 1
        self.position = {"ticket": self._ticket, "side": side, "lot": lot, "entry": entry,
                         "sl": round(entry - sign * sl_pts, 2), "tp": round(entry + sign * sl_pts * rr, 2),
                         "profit": 0.0, "trigger": "MANUAL" if manual else trigger, "risk": round(risk_usd, 2),
                         "be": False, "opened_utc": iso_z(self.now()), "manual": manual}
        self.day["trades"] += 1
        p = self.position
        self.emit("trade_opened", 0, "info",
                  {"symbol": self.symbol, "ticket": p["ticket"], "side": side, "entry": entry, "sl": p["sl"], "tp": p["tp"],
                   "lot": lot, "risk_usd": p["risk"], "risk_pct": round(risk_pct, 2), "trigger": p["trigger"],
                   "mark": "FU", "fu_class": "B", "type": "WITH", "manual": manual, "weak": False, "strength": 2,
                   "zone_lo": round(entry - sl_pts - 1, 2) if side == "BUY" else round(entry + 1, 2),
                   "zone_hi": round(entry - 1, 2) if side == "BUY" else round(entry + sl_pts + 1, 2), "score": 4,
                   "srcs": "H1 OB + M15 FU", "htf": True, "session": "LONDON", "rr": rr, "sl_src": "True Stop за зоной",
                   "sl_pts": sl_pts, "tp_pts": round(sl_pts * rr, 1), "shot": ""},
                  f"{'🟢' if side == 'BUY' else '🔴'} {self.symbol} {side} · сделка {self.day['trades']}/{self.set['max_trades']}")
        return p

    def set_price(self, bid: float, spread: float = 0.19) -> None:
        self.bid, self.ask = bid, round(bid + spread, 2)
        p = self.position
        if p:
            sign = 1 if p["side"] == "BUY" else -1
            cur = self.bid if p["side"] == "BUY" else self.ask
            p["profit"] = round(sign * (cur - p["entry"]) * p["lot"] * POINT_USD_PER_LOT, 2)

    def close_position(self, reason: str = "TP", price: float | None = None) -> dict | None:
        p = self.position
        if not p:
            return None
        sign = 1 if p["side"] == "BUY" else -1
        price = price if price is not None else (self.bid if p["side"] == "BUY" else self.ask)
        pts = round(sign * (price - p["entry"]), 2)
        usd = round(pts * p["lot"] * POINT_USD_PER_LOT, 2)
        self.balance += usd
        self.day["pts"] = round(self.day["pts"] + pts, 2)
        self.day["usd"] = round(self.day["usd"] + usd, 2)
        self.day["loss_streak"] = self.day["loss_streak"] + 1 if usd < 0 else 0
        dur = (self.now() - parse_iso(p["opened_utc"])).total_seconds() / 60
        rr_real = round(usd / p["risk"], 2) if p["risk"] else 0
        rec = {"date_open": local_str(parse_iso(p["opened_utc"]), TZ), "date_close": local_str(self.now(), TZ),
               "ticket": p["ticket"], "side": p["side"], "type": "WITH", "trigger": p["trigger"], "mark": "",
               "fu_class": "B", "score": 4, "zone_sources": "H1 OB + M15 FU", "zone_width": 3.5,
               "session": "LONDON", "entry": p["entry"], "sl": p["sl"], "tp": p["tp"], "lot": p["lot"],
               "risk_usd": p["risk"], "result_pts": pts, "result_usd": usd, "rr_real": rr_real, "exit": reason,
               "strength": 2, "weak": "false", "manual": str(p["manual"]).lower(), "duration_min": round(dur)}
        append_trade(self.paths.trades_csv, rec)
        self.position = None
        self.emit("trade_closed", 0, "info",
                  {"ticket": rec["ticket"], "side": rec["side"], "entry": rec["entry"], "exit_price": price,
                   "pts": pts, "usd": usd, "exit": reason, "duration_min": round(dur), "trigger": rec["trigger"],
                   "shot": ""}, f"{reason} · {self.symbol} {rec['side']} закрыта")
        return rec

    def alert(self, key: str, state: str, severity: str, text: str) -> None:
        if state == "open":
            self.alerts[key] = {"severity": severity, "text": text}
        else:
            self.alerts.pop(key, None)
        self.emit("alert", 0, severity, {"key": key, "state": state, "severity": severity}, text)

    # ---------- команды ----------
    def process_commands(self) -> int:
        """Выполняет файлы cmd/*.json по порядку ts_utc, как это делает OnTimer робота."""
        done = 0
        items = []
        for p in sorted(self.paths.cmd_dir.glob("*.json")):
            try:
                items.append((json.loads(p.read_text(encoding="utf-8")), p))
            except (json.JSONDecodeError, OSError):
                p.unlink(missing_ok=True)
        items.sort(key=lambda it: it[0].get("ts_utc", ""))
        for body, path in items:
            ok, msg, extra = self._run(body)
            path.unlink(missing_ok=True)
            self.emit("cmd_result", 0, "info", {"id": body.get("id"), "ok": ok, "msg": msg, **extra},
                      (f"✔️ Готово: {msg}" if ok else f"✖️ Не выполнено: {msg}"))
            done += 1
        return done

    def _run(self, body: dict) -> tuple[bool, str, dict]:
        if not self.key:
            return False, "команды не принимаются: InpBridgeKey пуст", {}
        if body.get("key") != self.key:
            return False, "неверный ключ моста", {}
        try:
            if parse_iso(body["expires_utc"]) < self.now():
                return False, "команда устарела", {}
        except (KeyError, ValueError):
            return False, "нет срока действия команды", {}
        cmd, args = body.get("cmd", ""), body.get("args") or {}
        ok, msg = validate_command(cmd, args)
        if not ok:
            return False, msg, {}
        return getattr(self, f"_cmd_{cmd}")(args)

    def _cmd_ping(self, a):
        return True, "pong", {}

    def _cmd_status(self, a):
        self.write_status()
        return True, "статус обновлён", {}

    def _cmd_pause(self, a):
        self.set["paused"] = True
        self.pause_reason = "Telegram"
        return True, "пауза включена, новые входы остановлены", {}

    def _cmd_resume(self, a):
        self.set["paused"] = False
        self.pause_reason = ""
        return True, "пауза снята, снова ищу входы", {}

    def _cmd_close(self, a):
        if not self.position:
            return False, "открытой позиции нет", {}
        rec = self.close_position("MANUAL")
        return True, f"позиция закрыта: {rec['result_usd']:+.2f} USD", {}

    def _cmd_be(self, a):
        p = self.position
        if not p:
            return False, "открытой позиции нет", {}
        if p["profit"] <= 0:
            return False, "позиция не в прибыли, безубыток не поставить", {}
        p["sl"], p["be"] = p["entry"], True
        self.emit("trade_be", 0, "info", {"ticket": p["ticket"], "sl": p["sl"]}, "⚪ Стоп перенесён в безубыток")
        return True, f"стоп в безубытке: {p['sl']}", {}

    def _cmd_modify(self, a):
        p = self.position
        if not p:
            return False, "открытой позиции нет", {}
        cur = self.bid if p["side"] == "BUY" else self.ask
        buy = p["side"] == "BUY"
        if "sl" in a and not (a["sl"] < cur if buy else a["sl"] > cur):
            return False, "стоп с неправильной стороны от цены", {}
        if "tp" in a and not (a["tp"] > cur if buy else a["tp"] < cur):
            return False, "тейк с неправильной стороны от цены", {}
        for k in ("sl", "tp"):
            if k in a:
                p[k] = float(a[k])
        self.emit("trade_modified", 0, "info", {"ticket": p["ticket"], "sl": p["sl"], "tp": p["tp"]}, "Стоп/тейк изменены")
        return True, f"новые уровни: SL {p['sl']}, TP {p['tp']}", {}

    def _cmd_set(self, a):
        side_off = {"buy": a.get("buy", self.set["buy"]), "sell": a.get("sell", self.set["sell"])}
        if not side_off["buy"] and not side_off["sell"]:
            return False, "нельзя выключить обе стороны", {}
        for k, v in a.items():
            self.set[k] = int(v) if k == "max_trades" else v
        self._save_settings()
        parts = [f"{k}={v}" for k, v in a.items()]
        return True, "настройки: " + ", ".join(parts), {}

    def _cmd_open(self, a):
        if self.position:
            return False, "позиция уже открыта", {}
        if self.day["trades"] >= self.set["max_trades"]:
            return False, "дневной лимит сделок исчерпан", {}
        if self.ask - self.bid > 1.0:
            return False, "спред слишком большой", {}
        p = self.open_position(a["side"], float(a["sl_pts"]), manual=True, risk_pct=a.get("risk_pct"), rr=a.get("rr"))
        return True, f"открыта {p['side']} {p['lot']} лота, SL {p['sl']}, TP {p['tp']}", {}

    def _cmd_reset(self, a):
        paused = self.set["paused"]
        self.set = dict(DEFAULTS)
        self.set["paused"] = paused           # как в роботе: reset возвращает параметры, паузу не снимает
        self._save_settings()
        return True, "настройки возвращены к значениям из входных параметров", {}

    def _cmd_chart(self, a):
        name = f"{self.seq + 1}.png"
        self.paths.shots_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.shots_dir / name).write_bytes(PNG_1x1)
        return True, f"скрин {a.get('tf', 'M5')}", {"shot": name}

    # ---------- один такт ----------
    def tick(self, status_every: float = 10.0) -> None:
        self.process_commands()
        t = time.monotonic()
        if t - self._status_at >= status_every:
            self._status_at = t
            self.write_status()


def _demo_step(ea: FakeEA, rnd: random.Random) -> None:
    ea.set_price(ea.bid + rnd.uniform(-0.8, 0.8))
    p = ea.position
    if p:
        sign = 1 if p["side"] == "BUY" else -1
        cur = ea.bid if p["side"] == "BUY" else ea.ask
        if sign * (cur - p["tp"]) >= 0:
            ea.close_position("TP", p["tp"])
        elif sign * (p["sl"] - cur) >= 0:
            ea.close_position("SL", p["sl"])
    elif not ea.set["paused"] and ea.day["trades"] < ea.set["max_trades"] and rnd.random() < 0.02:
        ea.open_position(rnd.choice(["BUY", "SELL"]), 5.5)


def main() -> None:
    ap = argparse.ArgumentParser(description="Симулятор робота MrCasino v3.4 (мост)")
    ap.add_argument("--root", default="data/bridge")
    ap.add_argument("--robot", default="12345678_777001")
    ap.add_argument("--key", default="test-key")
    ap.add_argument("--demo", action="store_true", help="случайные цены и сделки")
    args = ap.parse_args()
    ea = FakeEA(BridgePaths(Path(args.root), args.robot), args.key)
    ea.start()
    rnd = random.Random(7)
    print(f"fake_ea: {ea.paths.base}  (Ctrl+C - стоп)")
    try:
        while True:
            ea.tick()
            if args.demo:
                _demo_step(ea, rnd)
            time.sleep(1)
    except KeyboardInterrupt:
        ea.stop("остановлен вручную")


if __name__ == "__main__":
    main()
