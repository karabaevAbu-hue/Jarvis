"""Тексты сообщений: статус, сделки, события робота (раздел 7 ТЗ). Без ИИ, только по шаблонам.

Время в тексте — по Бишкеку, цены с точностью символа, деньги с 2 знаками и валютой счёта.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from agent.bridge.events import Event
from agent.bridge.protocol import Status
from agent.util import parse_iso

DIR_ICON = {"BUY": "🟢", "SELL": "🔴"}


def money(x: float, cur: str = "USD") -> str:
    return f"{x:+.2f} {cur}".replace("-", "−")


def _hm(iso: str | None, tz: ZoneInfo) -> str:
    return parse_iso(iso).astimezone(tz).strftime("%H:%M") if iso else "—"


def status_text(st: Status | None, tz: ZoneInfo) -> str:
    if st is None:
        return "Робот не отвечает: status.json не найден. MT5 закрыт или робот не стоит на графике."
    age = st.age_sec
    cur = st.get("account", "currency", default="USD")
    lines = []
    if age > 60:
        lines.append(f"⚠️ Статус устарел на {int(age)} с: MT5 завис или робот снят с графика.")
    state = "на паузе" if st.paused else "работает"
    d = st.get("day", default={}) or {}
    lines.append(f"Робот {state}. Сегодня {d.get('trades', 0)} из {d.get('max_trades', '?')} сделок, "
                 f"день {money(d.get('usd', 0.0), cur)} ({d.get('pts', 0.0):+.1f} pt).")
    pos = st.position
    if pos:
        lines.append(f"Открыта {pos['side']} {pos['lot']} лота с {pos['entry']}: SL {pos['sl']}, TP {pos['tp']}, "
                     f"сейчас {money(pos['profit'], cur)}.")
    else:
        zones = [z for z in (st.get("zones", default=[]) or []) if z.get("status") == "CONFIRMED"]
        if zones:
            z = min(zones, key=lambda q: abs(q.get("dist", 0)))
            lines.append(f"Ждёт в {z['dir']}-зоне {z['lo']:g}–{z['hi']:g} (балл {z['score']}).")
        else:
            lines.append("Позиции нет, подтверждённых зон рядом нет.")
    lines.append(f"Риска на день осталось {d.get('risk_left', 0):.2f} {cur}.")
    if d.get("blocked"):
        lines.append(f"Блок: {d['blocked']}.")
    return "\n".join(lines)


def trades_text(trades: list[dict], cur: str = "USD") -> str:
    if not trades:
        return "Закрытых сделок нет."
    out = []
    for t in trades:
        icon = "✅" if t["result_usd"] > 0 else "🔴" if t["result_usd"] < 0 else "⚪"
        out.append(f"{icon} {str(t['date_close'])[11:16]} {t['side']} {t['exit']} {t['entry']:g}→"
                   f"{t['entry'] + (t['result_pts'] if t['side'] == 'BUY' else -t['result_pts']):g} "
                   f"{t['result_pts']:+.1f} pt {money(t['result_usd'], cur)} (триггер {t['trigger']})")
    total = sum(t["result_usd"] for t in trades)
    out.append(f"Итого: {len(trades)} сделок, {money(total, cur)}")
    return "\n".join(out)


def _label(ev: Event, multi: bool, server: str = "", login: str = "") -> str:
    return f"[{server} …{login[-4:]}] " if multi and login else ""


STARS = {1: "★", 2: "★★", 3: "★★★"}


def fmt_duration(minutes: float) -> str:
    m = int(minutes)
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"


def _tfs_from_srcs(srcs: str) -> str:
    found = [tf for tf in ("W1", "D1", "H4", "H1", "M15") if tf in srcs.split() or f"{tf} " in srcs or srcs.startswith(tf)]
    return " · ".join(found + ["M5"])


def trade_opened_text(d: dict, cur: str, status: Status | None) -> str:
    """Шаблон из раздела 7 ТЗ. Строки, для которых нет данных, пропускаются."""
    side = d.get("side", "")
    n = ""
    if status is not None:
        day = status.get("day", default={}) or {}
        if day.get("trades"):
            n = f" · сделка {day['trades']}/{day.get('max_trades', '?')}"
    head = f"{DIR_ICON.get(side, '')} {d.get('symbol', 'XAUUSD')} {side}" + (f" · {d['type']}" if d.get("type") else "") + n
    sl_pts, tp_pts = d.get("sl_pts"), d.get("tp_pts")
    sl_line = f"SL: {d.get('sl')}"
    if sl_pts is not None:
        sl_line += f" (−{sl_pts:g} pt" + (f", {d['sl_src']}" if d.get("sl_src") else "") + ")"
    tp_line = f"TP: {d.get('tp')}"
    if tp_pts is not None:
        rr = d.get("rr") or (tp_pts / sl_pts if sl_pts else 0)
        tp_line += f" (+{tp_pts:g} pt, RR 1:{rr:g})"
    pct = f" ({d['risk_pct']:.1f}%)" if d.get("risk_pct") is not None else ""
    lines = [head, f"Вход: {d.get('entry')}", sl_line, tp_line,
             f"Лот {d.get('lot')} · риск {d.get('risk_usd', 0):.2f} {cur}{pct}"]
    if d.get("zone_lo") is not None:
        lines += ["", f"✅ Зона {d['zone_lo']:.2f}–{d['zone_hi']:.2f} · балл {d.get('score', '?')}"
                  + (f" · {d['srcs']}" if d.get("srcs") else "")]
    if d.get("trigger") and d["trigger"] != "MANUAL":
        mark = f" ({d['mark']})" if d.get("mark") else ""
        fu = f", FU класс {d['fu_class']}" if d.get("fu_class") not in (None, "", "-") else ""
        lines.append(f"✅ Триггер {d['trigger']}{mark}{fu}")
    if d.get("htf"):
        lines.append("✅ HTF Re-test Level")
    if d.get("zone_lo") is not None:
        lines.append(f"ТФ: {_tfs_from_srcs(d.get('srcs', ''))} · сессия {d.get('session', '—')}")
        lines.append(f"Сила сигнала: {STARS.get(int(d.get('strength') or 1), '★')}")
    if d.get("weak"):
        lines.append("⚠️ Слабый сигнал")
    if d.get("manual"):
        lines.append("✋ Ручная сделка из Telegram")
    return "\n".join(lines)


def trade_closed_text(d: dict, cur: str, status: Status | None) -> str:
    ex = d.get("exit", "")
    head = {"TP": "✅ TP", "SL": "🔴 SL", "BE": "⚪ BE", "MANUAL": "✋ Закрыто из Telegram"}.get(ex, f"⏹ {ex}")
    lines = [f"{head} · {d.get('symbol', 'XAUUSD')} {d.get('side')} закрыта",
             f"{d.get('entry')} → {d.get('exit_price')} · {d.get('pts', 0):+.1f} pt · {money(d.get('usd', 0.0), cur)}",
             f"Длилась {fmt_duration(d.get('duration_min', 0))} · триггер {d.get('trigger', '—')}"]
    if status is not None:
        day = status.get("day", default={}) or {}
        if day:
            lines.append(f"День: {day.get('trades', 0)}/{day.get('max_trades', '?')} сделок · {day.get('pts', 0):+.1f} pt · "
                         f"{money(day.get('usd', 0.0), cur)} · риска осталось {day.get('risk_left', 0):.2f} {cur}")
    return "\n".join(lines)


def event_text(ev: Event, tz: ZoneInfo, cur: str = "USD", multi: bool = False, status: Status | None = None) -> str:
    """Текст события робота. Шаблон есть для известных типов, иначе берётся `text` от робота."""
    d = ev.data
    t = ev.type
    if t == "alert":
        key = d.get("key", "")
        if d.get("state") == "closed":
            return f"✅ Восстановлено: {ev.text or ALERT_TITLES.get(key, key)}"
        title = ALERT_TITLES.get(key, key)
        return ev.text if ev.text.startswith("🚨") else f"🚨 ТРЕВОГА: {title}\n{ev.text}".strip()
    if t == "trade_opened":
        return trade_opened_text(d, cur, status)
    if t == "trade_closed":
        return trade_closed_text(d, cur, status)
    if t == "trade_be":
        return f"⚪ Стоп перенесён в безубыток: {d.get('sl')}"
    if t == "trade_modified":
        return f"Стоп/тейк изменены: SL {d.get('sl')}, TP {d.get('tp')}"
    if t == "limit_reached":
        return ev.text or "⛔ Достигнут лимит, робот стоит до завтра."
    if t == "cmd_result":
        return ("✔️ Готово: " if d.get("ok") else "✖️ Не выполнено: ") + str(d.get("msg", ""))
    if t == "ea_started":
        return ev.text or "▶️ Робот запущен"
    if t == "ea_stopped":
        return f"⏹ Робот остановлен: {d.get('reason', '')}"
    if t == "bias_changed":
        return ev.text or f"🔄 Bias {d.get('tf', '')}: {d.get('from', '')} → {d.get('to', '')}"
    return ev.text or f"{t}"


ALERT_TITLES = {
    "disconnect": "нет связи с брокером",
    "algo_off": "алго-торговля выключена",
    "no_sl": "позиция без стопа",
    "orders_rejected": "3 отказа ордеров подряд, робот на паузе",
    "day_dd": "просадка за день",
    "day_limit": "дневной риск исчерпан",
    "spread": "спред выше нормы",
    "no_ticks": "нет тиков",
    "margin": "низкий уровень маржи",
    "friday_close": "позиция закрыта перед выходными",
    "mt5_down": "MT5 закрыт",
    "robot_silent": "робот молчит",
}

# критичные ключи, на которые звонит ассистент (раздел 13)
CRITICAL_KEYS = {"disconnect", "algo_off", "no_sl", "orders_rejected", "day_limit", "mt5_down", "robot_silent"}
