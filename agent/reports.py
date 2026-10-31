"""Отчёты: утренний план, зоны на завтра, итог дня, недельный разбор (раздел 13 ТЗ).

Цифры только из робота (status.json, trades.csv, события) и движка анализа. ИИ в недельном разборе
получает готовую статистику и объясняет её; если бюджет кончился, уходит таблица без разбора.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import timedelta

from agent.analysis.chart import render_chart
from agent.analysis.engine import render_text
from agent.bridge.events import read_recent
from agent.bridge.trades import GROUPINGS, compute, format_table, period_filter, read_trades
from agent.brain.llm import BudgetExhausted, LLMUnavailable
from agent.brain.tools import OutMsg, ToolContext, _run_analysis
from agent.templates import money
from agent.util import local_day

log = logging.getLogger("reports")

SESSION_TEXT = "Сессия 10:00–23:00"


async def _analysis(ctx: ToolContext):
    from agent.analysis.data_mt5 import MT5Unavailable
    try:
        res = await _run_analysis(ctx, None, None)
        frames = ctx.engine.last_frames  # type: ignore[union-attr]
        png = await asyncio.to_thread(render_chart, frames["H1"].df, res, None, None, None, "H1")
        return res, png, None
    except MT5Unavailable as e:
        return None, None, f"{e}"


def _zone_line(z) -> str:
    return f"{z.dir}-зона: {z.lo:.2f}–{z.hi:.2f} ({'CONF' if z.status == 'CONFIRMED' else 'NEW'}, балл {z.score})"


async def morning_plan(ctx: ToolContext) -> list[OutMsg]:
    now = ctx.now().astimezone(ctx.tz)
    st = ctx.status()
    res, png, err = await _analysis(ctx)
    sym = (st.get("symbol", "name") if st else None) or (res.symbol if res else "XAUUSD")
    lines = [f"☀️ План на {now:%d.%m} · {sym}"]
    bias = res.bias if res else (st.get("bias", default={}) if st else {}) or {}
    if bias:
        lines.append(f"Bias: D1 {bias.get('d1', '—')} · H4 {bias.get('h4', '—')}")
    if res:
        for d in ("SELL", "BUY"):
            z = next((z for z in res.zones if z.dir == d), None)
            if z:
                lines.append(_zone_line(z))
        lv = sorted({round(l.price, 2) for l in res.levels})
        if lv:
            lines.append("HTF-уровни H4: " + " / ".join(f"{p:g}" for p in lv))
    else:
        lines.append(f"Зоны движка недоступны: {err}")
    news = st.get("news") if st else None
    lines.append("Новости: " + (", ".join(map(str, news)) if news else "робот окна новостей не передаёт"))
    s = (st.get("settings", default={}) if st else {}) or {}
    if s:
        lines.append(f"Риск {s.get('risk_pct', '?'):g}% на сделку, {s.get('daily_risk_pct', '?'):g}% в день · "
                     f"до {s.get('max_trades', '?')} сделок · RR 1:{s.get('rr', '?'):g}")
    lines.append(SESSION_TEXT)
    if st is None:
        lines.append("⚠️ Робот не отвечает: MT5 закрыт или робот не на графике.")
    outs = [OutMsg("\n".join(lines))]
    if res:
        outs.append(OutMsg(render_text(res), photo=png))
    return outs


async def evening_zones(ctx: ToolContext) -> list[OutMsg]:
    res, png, err = await _analysis(ctx)
    if res is None:
        return [OutMsg(f"🌙 Зоны на завтра: {err}")]
    return [OutMsg("🌙 Зоны на завтра\n" + render_text(res), photo=png)]


async def daily_report(ctx: ToolContext) -> list[OutMsg]:
    now = ctx.now().astimezone(ctx.tz)
    today = local_day(ctx.now(), ctx.tz)
    from agent.bridge.trades import read_trades as _rt
    trades = period_filter(_rt(ctx.hub.paths.trades_csv) if ctx.hub and ctx.hub.paths else [], today, today)
    st = ctx.status()
    cur = st.get("account", "currency", default="USD") if st else "USD"
    sym = (st.get("symbol", "name") if st else None) or "XAUUSD"
    head = f"📊 Итог дня {now:%d.%m} · {sym}"
    if not trades:
        return [OutMsg(f"{head}\nСделок сегодня не было.")]
    s = compute(trades)
    cnt = Counter(t["exit"] for t in trades)
    max_t = (st.get("settings", "max_trades") if st else None) or "?"
    balance = st.get("account", "balance") if st else None
    start_bal = (balance - s.usd) if balance is not None else None
    pct = f" ({s.usd / start_bal * 100:+.1f}%)" if start_bal else ""
    wr = f" · винрейт {s.winrate:.0f}%" if s.winrate is not None else ""
    best = max(trades, key=lambda t: t["result_usd"])
    worst = min(trades, key=lambda t: t["result_usd"])
    lines = [head,
             f"Сделок {len(trades)}/{max_t}: {cnt.get('TP', 0)} TP, {cnt.get('SL', 0)} SL"
             + (f", {cnt.get('BE', 0)} BE" if cnt.get("BE") else "") + wr,
             f"{s.pts:+.1f} pt · {money(s.usd, cur)}{pct}",
             f"Лучшая: {best['trigger']} {best['side']} {best['result_pts']:+.1f} · "
             f"худшая: {worst['trigger']} {worst['side']} {worst['result_pts']:+.1f}"]
    if st:
        day = st.get("day", default={}) or {}
        dp = (st.get("settings", "daily_risk_pct") or 0)
        if start_bal and dp:
            limit = start_bal * dp / 100
            spent = max(0.0, limit - day.get("risk_left", limit))
            lines.append(f"Дневной риск: потрачено {spent:.2f} из {limit:.2f} {cur}")
    if ctx.hub and ctx.hub.paths:
        evs = [e for e in read_recent(ctx.hub.paths, 500, {"signal_skipped"}) if e.ts_local.startswith(today)]
        if evs:
            why = Counter((e.data.get("why") or e.data.get("reason") or e.text) for e in evs).most_common(1)[0][0]
            lines.append(f"Пропущено сигналов: {len(evs)} (чаще всего: {why})")
    if balance is not None:
        lines.append(f"Баланс: {balance:.2f} {cur}")
    return [OutMsg("\n".join(lines))]


async def weekly_report(ctx: ToolContext, llm=None, system_static: str = "") -> list[OutMsg]:
    """Недельный разбор: таблица по триггерам, сессиям, силе, слабым и обычным + разбор стопов от умной модели."""
    now = ctx.now().astimezone(ctx.tz)
    start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    trades = period_filter(read_trades(ctx.hub.paths.trades_csv) if ctx.hub and ctx.hub.paths else [], start)
    if not trades:
        return [OutMsg("📅 Недельный разбор: закрытых сделок за 7 дней нет.")]
    table = format_table(trades)
    s = compute(trades)
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "нет убытков"
    wr = f"{s.winrate:.0f}%" if s.winrate is not None else "—"
    head = (f"📅 Недельный разбор {start} — {now:%Y-%m-%d}\nСделок {s.n} · винрейт {wr} · {s.pts:+.1f} pt · "
            f"{s.usd:+.2f} USD · profit factor {pf} · средний RR {s.rr_avg:.2f}")
    weak = compute([t for t in trades if t["weak"]])
    norm = compute([t for t in trades if not t["weak"]])
    cmp_line = ""
    if weak.n:
        cmp_line = (f"\nСлабые сигналы: {weak.n} сделок, {weak.usd:+.2f} USD"
                    f"{' (мало данных)' if weak.few else ''}; обычные: {norm.n} сделок, {norm.usd:+.2f} USD."
                    f" {'Слабые окупаются.' if weak.usd > 0 else 'Слабые пока в минусе.'}")
    outs = [OutMsg(head + cmp_line + "\n```\n" + table + "\n```")]
    if llm is not None and llm.available:
        stops = [t for t in trades if t["exit"] == "SL"]
        prompt = ("Сделай недельный разбор для Абу в 6–10 строках: что работает, что нет, разбор стопов, "
                  "один вывод на следующую неделю. Только по данным ниже, цифры не придумывай, без призывов к "
                  "прибыли.\n\n" + head + cmp_line + "\n" + table + "\n\nСтопы:\n" +
                  "\n".join(f"{t['date_close']} {t['side']} триггер {t['trigger']} {t['type']} {t['session']} "
                            f"балл {t['score']} {t['zone_sources']} {t['result_pts']:+.1f} pt" for t in stops[:15]))
        try:
            async def no_tools(name, args):
                return {"error": "инструментов нет"}
            res = await llm.run_turn(kind="smart", system_static=system_static, system_dynamic="",
                                     messages=[{"role": "user", "content": prompt}], tools=[], dispatch=no_tools)
            if res.text:
                outs.append(OutMsg(res.text))
        except (BudgetExhausted, LLMUnavailable) as e:
            outs.append(OutMsg(f"Разбор ИИ недоступен ({type(e).__name__}), таблица выше."))
    return outs
