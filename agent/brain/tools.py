"""Инструменты модели (раздел 10 ТЗ).

Чтение: вызываются сразу, цифры берутся из робота, MT5 и движка анализа.
Действие: только предлагают (создают ожидающее «да»); выполняет код после подтверждения.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.analysis.chart import render_chart
from agent.analysis.engine import AnalysisEngine, compare_with_robot, render_text
from agent.bridge.events import read_recent
from agent.bridge.hub import BridgeHub
from agent.bridge.protocol import Status, validate_command
from agent.bridge.trades import compute, format_table, period_filter, read_trades
from agent.brain.confirm import ConfirmManager, ExecResult
from agent.config import Settings
from agent.storage.db import Database
from agent.templates import money, status_text, trades_text
from agent.util import local_day, utcnow


@dataclass
class OutMsg:
    """Что отправить владельцу: текст и/или картинка."""
    text: str = ""
    photo: bytes | None = None
    voice: bool = False


@dataclass
class ToolContext:
    settings: Settings
    db: Database
    hub: BridgeHub | None
    confirm: ConfirmManager
    engine: AnalysisEngine | None = None
    outbox: list[OutMsg] = field(default_factory=list)
    now: Callable[[], datetime] = utcnow
    log_path: Callable[[], Path | None] | None = None   # путь к журналу советника (только Windows)
    _last_analysis: tuple[float, Any] | None = None

    @property
    def tz(self):
        return self.settings.zone

    def status(self) -> Status | None:
        return self.hub.status() if self.hub else None


# ------------------------------------------------------------------ описания для модели
def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


READ_TOOLS = [
    {"name": "get_status", "description": "Состояние робота: жив ли, пауза, счёт, позиция, день, настройки, bias, зоны робота.",
     "input_schema": _obj({})},
    {"name": "get_price", "description": "Цена золота: bid, ask, спред, изменение за день.", "input_schema": _obj({})},
    {"name": "get_today", "description": "Сделки, закрытые сегодня, и итог дня.", "input_schema": _obj({})},
    {"name": "get_history", "description": "Последние n закрытых сделок.",
     "input_schema": _obj({"n": {"type": "integer", "minimum": 1, "maximum": 50}}, ["n"])},
    {"name": "get_stats", "description": "Статистика по закрытым сделкам: винрейт, pt, USD, profit factor, разбивки.",
     "input_schema": _obj({"period": {"type": "string", "enum": ["day", "week", "month", "all"]}}, ["period"])},
    {"name": "get_robot_zones", "description": "Зоны, которые сейчас видит робот (из status.json).", "input_schema": _obj({})},
    {"name": "analyze_zones",
     "description": ("Зоны BUY/SELL от движка анализа по Mr. Casino и книге. Работает и без робота. Готовый ответ с "
                     "цифрами и график уже уходят владельцу отдельным сообщением: не пересказывай цифры, "
                     "добавь 1-3 строки своего взгляда."),
     "input_schema": _obj({"tf": {"type": "string", "enum": ["M15", "H1", "H4", "D1"]},
                           "side": {"type": "string", "enum": ["BUY", "SELL"]}})},
    {"name": "get_chart", "description": "График с зонами (PNG) владельцу.",
     "input_schema": _obj({"tf": {"type": "string", "enum": ["M15", "H1", "H4", "D1"]},
                           "zones": {"type": "boolean"}})},
    {"name": "explain_skips", "description": "Почему робот пропустил входы (события signal_skipped).",
     "input_schema": _obj({"n": {"type": "integer", "minimum": 1, "maximum": 20}})},
    {"name": "get_events", "description": "Последние события робота.",
     "input_schema": _obj({"n": {"type": "integer", "minimum": 1, "maximum": 30}}, ["n"])},
    {"name": "get_logs", "description": "Последние строки журнала советника MT5.",
     "input_schema": _obj({"n": {"type": "integer", "minimum": 1, "maximum": 60}}, ["n"])},
    {"name": "get_news_windows", "description": "Окна новостей из настроек робота.", "input_schema": _obj({})},
]

ACTION_TOOLS = [
    {"name": "pause", "description": "Предложить паузу: робот перестанет искать новые входы.", "input_schema": _obj({})},
    {"name": "resume", "description": "Предложить снять паузу.", "input_schema": _obj({})},
    {"name": "close_position", "description": "Предложить закрыть позицию робота по рынку.", "input_schema": _obj({})},
    {"name": "move_be", "description": "Предложить перенести стоп в безубыток.", "input_schema": _obj({})},
    {"name": "modify_sl", "description": "Предложить новый стоп (цена).",
     "input_schema": _obj({"price": {"type": "number"}}, ["price"])},
    {"name": "modify_tp", "description": "Предложить новый тейк (цена).",
     "input_schema": _obj({"price": {"type": "number"}}, ["price"])},
    {"name": "set_risk", "description": "Предложить риск на сделку, % (0.1-5).",
     "input_schema": _obj({"pct": {"type": "number"}}, ["pct"])},
    {"name": "set_daily_risk", "description": "Предложить дневной риск, % (1-20).",
     "input_schema": _obj({"pct": {"type": "number"}}, ["pct"])},
    {"name": "set_max_trades", "description": "Предложить максимум сделок в день (1-10).",
     "input_schema": _obj({"n": {"type": "integer"}}, ["n"])},
    {"name": "set_rr", "description": "Предложить RR тейка (1-5).", "input_schema": _obj({"x": {"type": "number"}}, ["x"])},
    {"name": "set_weak", "description": "Предложить включить/выключить слабые сигналы.",
     "input_schema": _obj({"on": {"type": "boolean"}}, ["on"])},
    {"name": "set_side", "description": "Предложить разрешённые стороны сделок.",
     "input_schema": _obj({"buy": {"type": "boolean"}, "sell": {"type": "boolean"}}, ["buy", "sell"])},
    {"name": "manual_open", "description": "Предложить ручную сделку (стоп обязателен).",
     "input_schema": _obj({"side": {"type": "string", "enum": ["BUY", "SELL"]}, "sl_pts": {"type": "number"},
                           "risk": {"type": "number"}, "rr": {"type": "number"}}, ["side", "sl_pts"])},
    {"name": "reset_settings", "description": "Предложить вернуть настройки к значениям входных параметров.",
     "input_schema": _obj({})},
]

ALL_TOOLS = READ_TOOLS + ACTION_TOOLS
ACTION_NAMES = {t["name"] for t in ACTION_TOOLS}


# ------------------------------------------------------------------ действие -> команда роботу
def to_command(kind: str, a: dict) -> tuple[str, dict]:
    m: dict[str, Callable[[dict], tuple[str, dict]]] = {
        "pause": lambda a: ("pause", {}), "resume": lambda a: ("resume", {}),
        "close_position": lambda a: ("close", {}), "move_be": lambda a: ("be", {}),
        "modify_sl": lambda a: ("modify", {"sl": a["price"]}), "modify_tp": lambda a: ("modify", {"tp": a["price"]}),
        "set_risk": lambda a: ("set", {"risk_pct": a["pct"]}),
        "set_daily_risk": lambda a: ("set", {"daily_risk_pct": a["pct"]}),
        "set_max_trades": lambda a: ("set", {"max_trades": a["n"]}), "set_rr": lambda a: ("set", {"rr": a["x"]}),
        "set_weak": lambda a: ("set", {"weak": bool(a["on"])}),
        "set_side": lambda a: ("set", {"buy": bool(a["buy"]), "sell": bool(a["sell"])}),
        "manual_open": lambda a: ("open", {k: v for k, v in
                                           {"side": a.get("side"), "sl_pts": a.get("sl_pts"),
                                            "risk_pct": a.get("risk"), "rr": a.get("rr")}.items() if v is not None}),
        "reset_settings": lambda a: ("reset", {}),
    }
    if kind not in m:
        raise KeyError(kind)
    return m[kind](a)


def make_executor(hub: BridgeHub, direct_close: Callable[[], Awaitable[ExecResult]] | None = None,
                  timeout: float = 10.0):
    """Исполнитель для ConfirmManager: команда роботу и ожидание cmd_result.

    Если робот не ответил, а нужно закрыть позицию, и Абу уже сказал «да», — закрываем напрямую через
    пакет MetaTrader5 (единственный случай прямой сделки, раздел 4 ТЗ).
    """
    async def run(kind: str, args: dict) -> ExecResult:
        cmd, cargs = to_command(kind, args)
        res = await hub.command(cmd, cargs, timeout=timeout)
        if res.timeout and kind == "close_position" and direct_close is not None:
            direct = await direct_close()
            return ExecResult(direct.ok, "робот не ответил, закрыл напрямую через MT5: " + direct.msg)
        return ExecResult(res.ok, res.msg)
    return run


# ------------------------------------------------------------------ тексты вопросов «да / нет»
def describe_action(kind: str, a: dict, st: Status | None) -> str:
    cur = st.get("account", "currency", default="USD") if st else "USD"
    pos = st.position if st else None
    sett = (st.get("settings", default={}) if st else {}) or {}
    day = (st.get("day", default={}) if st else {}) or {}
    tail = " Да / нет"
    warn = ""
    if kind == "pause":
        return "Поставить паузу? Робот перестанет искать новые входы, открытую позицию ведёт как обычно." + tail
    if kind == "resume":
        return "Снять паузу и снова искать входы?" + tail
    if kind == "close_position":
        return (f"Закрыть {pos['side']} {pos['lot']} по рынку? Сейчас {money(pos['profit'], cur)}." + tail)
    if kind == "move_be":
        return f"Перенести стоп в безубыток ({pos['entry']})? Сейчас {money(pos['profit'], cur)}." + tail
    if kind in ("modify_sl", "modify_tp"):
        key = "sl" if kind == "modify_sl" else "tp"
        name = "Стоп" if key == "sl" else "Тейк"
        return f"{name} {pos['side']} с {pos[key]} на {a['price']:g}? Вход {pos['entry']}." + tail
    if kind == "set_risk":
        daily = sett.get("daily_risk_pct")
        if daily and a["pct"] > daily:
            warn = f"\n⚠️ Это выше дневного лимита {daily:g}%."
        return f"Поставить риск {a['pct']:g}% на сделку (сейчас {sett.get('risk_pct', '?')}%)?{warn}" + tail
    if kind == "set_daily_risk":
        r = sett.get("risk_pct")
        if r and a["pct"] < r:
            warn = f"\n⚠️ Меньше риска на сделку ({r:g}%): робот будет упираться в дневной лимит."
        return f"Поставить дневной риск {a['pct']:g}% (сейчас {sett.get('daily_risk_pct', '?')}%)?{warn}" + tail
    if kind == "set_max_trades":
        return f"Поставить максимум {a['n']} сделок в день (сейчас {sett.get('max_trades', '?')})?" + tail
    if kind == "set_rr":
        return f"Поставить RR 1:{a['x']:g} (сейчас 1:{sett.get('rr', '?')})?" + tail
    if kind == "set_weak":
        return f"{'Включить' if a['on'] else 'Выключить'} слабые сигналы (сейчас {'вкл' if sett.get('weak') else 'выкл'})?" + tail
    if kind == "set_side":
        sides = [n for n, on in (("BUY", a["buy"]), ("SELL", a["sell"])) if on]
        return f"Разрешить стороны: {' и '.join(sides)}?" + tail
    if kind == "reset_settings":
        return "Вернуть все настройки к значениям входных параметров?" + tail
    if kind == "manual_open":
        acc = st.get("account", default={}) if st else {}
        sym = st.get("symbol", default={}) if st else {}
        side, slp = a["side"], float(a["sl_pts"])
        rr = float(a.get("rr") or sett.get("rr") or 2.0)
        rp = float(a.get("risk") or sett.get("risk_pct") or 1.0)
        entry = sym.get("ask") if side == "BUY" else sym.get("bid")
        risk_usd = acc.get("balance", 0.0) * rp / 100.0
        lot = max(0.01, round(risk_usd / (slp * 100.0), 2)) if slp else 0
        sign = 1 if side == "BUY" else -1
        if entry:
            lvl = f"по {entry:g}: SL {entry - sign * slp:.2f}, TP {entry + sign * slp * rr:.2f}"
        else:
            lvl = f"SL {slp:g} pt, RR 1:{rr:g}"
        if day.get("loss_streak", 0) >= 2:
            warn += f"\n⚠️ Сегодня уже {day['loss_streak']} стопа подряд."
        if day.get("risk_left") is not None and risk_usd > day["risk_left"]:
            warn += f"\n⚠️ Риск {risk_usd:.2f} {cur} больше остатка дневного лимита {day['risk_left']:.2f} {cur}."
        return (f"Открыть {side} {lvl} (стоп {slp:g} pt), лот ≈{lot}, риск ≈{risk_usd:.2f} {cur} ({rp:g}%)? "
                f"Ручная сделка робот ведёт как свою.{warn}" + tail)
    return "Выполнить действие?" + tail


def precheck(kind: str, a: dict, st: Status | None) -> str | None:
    """Проверки до вопроса «да»: ошибки видны сразу, а не после согласия. None = можно спрашивать."""
    if st is None:
        return "Робот не отвечает, команду отправить некуда."
    pos = st.position
    if kind in ("close_position", "move_be", "modify_sl", "modify_tp") and not pos:
        return "Открытой позиции нет."
    if kind == "move_be" and pos["profit"] <= 0:
        return "Позиция не в прибыли, стоп в безубыток не поставить."
    if kind in ("modify_sl", "modify_tp"):
        sym = st.get("symbol", default={}) or {}
        cur = sym.get("bid") if pos["side"] == "BUY" else sym.get("ask")
        buy = pos["side"] == "BUY"
        p = a["price"]
        if kind == "modify_sl" and cur and not (p < cur if buy else p > cur):
            return "Стоп с неправильной стороны от цены."
        if kind == "modify_tp" and cur and not (p > cur if buy else p < cur):
            return "Тейк с неправильной стороны от цены."
    if kind == "manual_open":
        if pos:
            return "Позиция уже открыта, вторую робот не откроет."
        day = st.get("day", default={}) or {}
        if day.get("trades", 0) >= day.get("max_trades", 99):
            return "Дневной лимит сделок исчерпан."
    if kind == "pause" and st.paused:
        return "Пауза уже включена."
    if kind == "resume" and not st.paused:
        return "Робот и так не на паузе."
    try:
        cmd, cargs = to_command(kind, a)
    except (KeyError, TypeError):
        return "Не хватает параметров."
    ok, msg = validate_command(cmd, cargs)
    return None if ok else msg


# ------------------------------------------------------------------ чтение
def _status_dict(st: Status | None) -> dict:
    if st is None:
        return {"error": "status.json не найден: MT5 закрыт или робот не на графике"}
    r = st.raw
    return {"age_sec": round(st.age_sec), "version": r.get("version"), "paused": r.get("paused"),
            "pause_reason": r.get("pause_reason"), "account": r.get("account"), "symbol": r.get("symbol"),
            "session": r.get("session"), "bias": r.get("bias"), "day": r.get("day"), "position": r.get("position"),
            "settings": r.get("settings"), "terminal": r.get("terminal"), "last_skip": r.get("last_skip"),
            "robot_zones": r.get("zones"), "bot_alive": (r.get("bridge") or {}).get("bot_alive")}


async def _tool_get_status(ctx: ToolContext, a: dict) -> dict:
    d = _status_dict(ctx.status())
    d["summary"] = status_text(ctx.status(), ctx.tz)
    return d


async def _tool_get_price(ctx: ToolContext, a: dict) -> dict:
    st = ctx.status()
    if st is None:
        return {"error": "нет данных от робота"}
    s = st.get("symbol", default={}) or {}
    return {"symbol": s.get("name"), "bid": s.get("bid"), "ask": s.get("ask"), "spread": s.get("spread"),
            "market_open": s.get("market_open")}


def _trades(ctx: ToolContext) -> list[dict]:
    if not ctx.hub:
        return []
    return read_trades(ctx.hub.paths.trades_csv)


async def _tool_get_today(ctx: ToolContext, a: dict) -> dict:
    today = local_day(ctx.now(), ctx.tz)
    tr = period_filter(_trades(ctx), today, today)
    st = ctx.status()
    cur = st.get("account", "currency", default="USD") if st else "USD"
    return {"date": today, "count": len(tr), "text": trades_text(tr, cur), "day": (st.get("day") if st else None)}


async def _tool_get_history(ctx: ToolContext, a: dict) -> dict:
    tr = _trades(ctx)[-int(a.get("n", 10)):]
    return {"count": len(tr), "text": trades_text(tr)}


async def _tool_get_stats(ctx: ToolContext, a: dict) -> dict:
    period = a.get("period", "all")
    now = ctx.now().astimezone(ctx.tz)
    start = {"day": now, "week": now - timedelta(days=7), "month": now - timedelta(days=30)}.get(period)
    tr = _trades(ctx)
    if start is not None:
        tr = period_filter(tr, start.strftime("%Y-%m-%d"))
    s = compute(tr)
    return {"period": period, "n": s.n, "winrate": s.winrate, "pts": round(s.pts, 1), "usd": round(s.usd, 2),
            "rr_avg": round(s.rr_avg, 2), "profit_factor": s.profit_factor, "few_data": s.few,
            "table": format_table(tr)}


async def _tool_get_robot_zones(ctx: ToolContext, a: dict) -> dict:
    st = ctx.status()
    return {"zones": (st.get("zones", default=[]) if st else []), "bias": (st.get("bias") if st else None)}


async def _run_analysis(ctx: ToolContext, tf: str | None, side: str | None):
    if ctx.engine is None:
        raise RuntimeError("движок анализа не подключён")
    res = await asyncio.to_thread(ctx.engine.analyze, tf, side)
    st = ctx.status()
    if st is not None and st.age_sec < 120:
        res.robot_diff = compare_with_robot(res, st.get("zones", default=[]) or [])
    return res


async def _tool_analyze_zones(ctx: ToolContext, a: dict) -> dict:
    from agent.analysis.data_mt5 import MT5Unavailable
    tf, side = a.get("tf"), a.get("side")
    try:
        res = await _run_analysis(ctx, tf, side)
    except MT5Unavailable as e:
        return {"error": f"{e}. Честно: MT5 не запущен, зоны посмотреть не могу."}
    text = render_text(res)
    png = None
    try:
        frames = ctx.engine.last_frames  # type: ignore[union-attr]
        png = await asyncio.to_thread(render_chart, frames[tf or "H1"].df, res, None, None, None, tf or "H1")
    except Exception:
        png = None
    ctx.outbox.append(OutMsg(text=text, photo=png))
    ctx._last_analysis = (ctx.now().timestamp(), res)
    out = {"sent_to_owner": True, "bias": res.bias, "price": res.price, "session": res.session,
           "zones": [z.to_dict() for z in res.zones], "figures": [f.text for f in res.figures[:3]],
           "candle_models": res.candle_models[:3], "robot_diff": res.robot_diff, "warnings": res.warnings}
    if png:
        out["_image_png"] = png        # картинка уходит модели, не владельцу второй раз
    return out


async def _tool_get_chart(ctx: ToolContext, a: dict) -> dict:
    from agent.analysis.data_mt5 import MT5Unavailable
    tf = a.get("tf", "H1")
    try:
        res = await _run_analysis(ctx, tf if tf != "H1" else None, None)
        frames = ctx.engine.last_frames  # type: ignore[union-attr]
        zones = res.zones if a.get("zones", True) else []
        png = await asyncio.to_thread(render_chart, frames[tf].df, res, zones, res.liquidity, res.price, tf)
    except MT5Unavailable as e:
        return {"error": f"{e}. MT5 не запущен, график не нарисовать."}
    ctx.outbox.append(OutMsg(text=f"График {res.symbol} {tf}", photo=png))
    return {"sent_to_owner": True}


async def _tool_explain_skips(ctx: ToolContext, a: dict) -> dict:
    if not ctx.hub:
        return {"error": "нет моста"}
    evs = read_recent(ctx.hub.paths, int(a.get("n", 8)), {"signal_skipped", "analysis_skip"})
    return {"skips": [{"time": e.ts_local, "type": e.type, "text": e.text, "data": e.data} for e in evs]}


async def _tool_get_events(ctx: ToolContext, a: dict) -> dict:
    if not ctx.hub:
        return {"error": "нет моста"}
    evs = read_recent(ctx.hub.paths, int(a.get("n", 10)))
    return {"events": [{"seq": e.seq, "time": e.ts_local, "type": e.type, "severity": e.severity, "text": e.text}
                       for e in evs]}


async def _tool_get_logs(ctx: ToolContext, a: dict) -> dict:
    path = ctx.log_path() if ctx.log_path else None
    if not path or not Path(path).exists():
        return {"error": "журнал советника недоступен (нужен запущенный MT5 на Windows)"}
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16", errors="replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    return {"lines": text.splitlines()[-int(a.get("n", 20)):]}


async def _tool_get_news_windows(ctx: ToolContext, a: dict) -> dict:
    st = ctx.status()
    news = st.get("news") if st else None
    if news is None:
        return {"error": "робот не передаёт окна новостей: поля news в status.json нет"}
    return {"news": news}


READ_DISPATCH: dict[str, Callable[[ToolContext, dict], Awaitable[dict]]] = {
    "get_status": _tool_get_status, "get_price": _tool_get_price, "get_today": _tool_get_today,
    "get_history": _tool_get_history, "get_stats": _tool_get_stats, "get_robot_zones": _tool_get_robot_zones,
    "analyze_zones": _tool_analyze_zones, "get_chart": _tool_get_chart, "explain_skips": _tool_explain_skips,
    "get_events": _tool_get_events, "get_logs": _tool_get_logs, "get_news_windows": _tool_get_news_windows,
}


# ------------------------------------------------------------------ вызов
async def dispatch(ctx: ToolContext, name: str, args: dict) -> dict:
    """Вызов инструмента. Для инструментов на действие ничего не выполняется: создаётся ожидающее «да»,
    а в результате лежит `final_text` — точные цифры, которые код сам покажет владельцу."""
    try:
        if name in ACTION_NAMES:
            return await propose_action(ctx, name, args)
        fn = READ_DISPATCH.get(name)
        if fn is None:
            return {"error": f"неизвестный инструмент {name}"}
        return await fn(ctx, args)
    except Exception as e:  # ошибка инструмента не должна ронять диалог
        return {"error": f"{type(e).__name__}: {e}"}


async def propose_action(ctx: ToolContext, kind: str, args: dict) -> dict:
    st = ctx.status()
    err = precheck(kind, args, st)
    if err:
        return {"status": "rejected", "reason": err, "final_text": f"✖️ {err}"}
    prompt = describe_action(kind, args, st)
    await ctx.confirm.propose(kind, args, prompt)
    return {"status": "awaiting_confirmation", "final_text": prompt}
