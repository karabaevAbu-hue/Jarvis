"""Оркестратор анализа «дай зону»: данные -> bias -> ликвидность -> зоны -> план -> текст.

Цифры зон берутся только отсюда. ИИ не придумывает границы, он проверяет и объясняет.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.analysis import figures as figmod
from agent.analysis import liquidity as liqmod
from agent.analysis import params as P
from agent.analysis.data_mt5 import CandleSource, MT5Unavailable, load_frames
from agent.analysis.levels import calc_bias, htf_retest_levels, session_name
from agent.analysis.models import TF_RANK, AnalysisResult, Zone
from agent.analysis.patterns import Candles, candle_models
from agent.analysis.zones import build_zones, make_plan


class AnalysisEngine:
    def __init__(self, source: CandleSource, symbol: str = "", rr: float = P.RR):
        self.source = source
        self.symbol = symbol
        self.rr = rr
        self.last_frames: dict[str, Candles] = {}

    def analyze(self, tf: str | None = None, side: str | None = None,
                now: datetime | None = None) -> AnalysisResult:
        symbol = self.source.find_symbol(self.symbol)
        if not symbol:
            raise MT5Unavailable("символ золота не найден в MT5")
        raw = load_frames(self.source, symbol)
        frames = {k: Candles(v, k) for k, v in raw.items() if len(v) >= 20}
        warnings: list[str] = []
        for need in ("D1", "H4", "H1", "M15"):
            if need not in frames:
                warnings.append(f"нет данных {need}: анализ неполный")
        if "H1" not in frames:
            raise MT5Unavailable("нет свечей H1")
        tick = self.source.tick(symbol)
        price = float(tick["bid"]) if tick else float(frames["H1"].c[-1])
        atr_h1 = frames["H1"].atr()

        bias = {}
        for k, name in (("d1", "D1"), ("h4", "H4")):
            bias[k] = calc_bias(frames[name])[0] if name in frames else "NEUTRAL"

        liq = liqmod.collect(frames)
        levels = htf_retest_levels(frames["H4"], price) if "H4" in frames else []
        imbs = []
        for name in ("H4", "H1"):
            if name in frames:
                imbs += liqmod.imbalances(frames[name])
        zones = build_zones(frames, price, bias, liq, levels, imbs, atr_h1)
        if side in ("BUY", "SELL"):
            zones = [z for z in zones if z.dir == side]
        if tf and tf in TF_RANK:
            zones = [z for z in zones if any(s.tf == tf for s in z.sources)]
        for z in zones:
            make_plan(z, liq, atr_h1, self.rr)

        figs = []
        for name in ("H1", "H4"):
            if name in frames:
                figs += figmod.detect(frames[name])
        models = []
        for name in ("M15", "H1"):
            if name in frames:
                models += [f"{name} {m}" for m in candle_models(frames[name])]

        when = now or datetime.now(timezone.utc)
        self.last_frames = frames
        return AnalysisResult(symbol=symbol, price=price, bias=bias, zones=zones, liquidity=liq, levels=levels,
                              figures=figs, imbalances=imbs, candle_models=models, session=session_name(when),
                              tf=tf or "H1", frames_ok={k: v.n for k, v in frames.items()}, warnings=warnings)


# ------------------------------------------------------------------ сверка с роботом
def compare_with_robot(result: AnalysisResult, robot_zones: list[dict], tol: float = 1.0) -> list[str]:
    """Расхождение зон движка и робота больше tol пунктов — показывается с причиной (раздел 12.7)."""
    out: list[str] = []
    for rz in robot_zones:
        d = rz.get("dir")
        same = [z for z in result.zones if z.dir == d and z.lo <= rz["hi"] and z.hi >= rz["lo"]]
        if not same:
            out.append(f"Робот видит {d} {rz['lo']:.1f}-{rz['hi']:.1f}, движок такую зону не нашёл "
                       f"(нет FU класса A/B в этих границах или зона вне окна {P.MAX_ZONE_DIST_ATR:g} ATR)")
            continue
        z = min(same, key=lambda q: abs(q.lo - rz["lo"]) + abs(q.hi - rz["hi"]))
        dlo, dhi = abs(z.lo - rz["lo"]), abs(z.hi - rz["hi"])
        if max(dlo, dhi) > tol:
            why = "границы собраны по-разному: движок берёт тень FU, уточнённую внутри OB"
            if abs(z.width - (rz["hi"] - rz["lo"])) > tol:
                why = f"разная ширина: у движка {z.width:.1f} pt, у робота {rz['hi'] - rz['lo']:.1f} pt"
            out.append(f"{d}: робот {rz['lo']:.1f}-{rz['hi']:.1f}, движок {z.lo:.1f}-{z.hi:.1f}. {why}")
    for z in result.zones:
        if not any(rz.get("dir") == z.dir and z.lo <= rz["hi"] and z.hi >= rz["lo"] for rz in robot_zones):
            out.append(f"Движок показывает {z.dir} {z.lo:.1f}-{z.hi:.1f}, а робот эту зону не держит")
    return out


# ------------------------------------------------------------------ текст
def _entry_line(z: Zone) -> str:
    pl = z.plan
    assert pl is not None
    if z.hcs:
        how = "Вход: по HCS / FU в зоне"
    elif z.status == "CONFIRMED":
        how = "Вход: после FU или Negation в зоне"
    else:
        how = "Вход только по HCS или Strong Negation"
    rr = f"1:{pl.risk_pts and round(abs(pl.tp - pl.entry_ref) / pl.risk_pts):g}"
    lal = f", LAL {pl.tp_lal:g}" if pl.tp_lal else ""
    word = "ниже" if z.dir == "BUY" else "выше"
    wide = f" ⚠️ стоп {pl.risk_pts:g} > {P.SL_MAX:g}" if pl.stop_too_wide else ""
    return f"{how} · SL {pl.sl:.1f} · TP {pl.tp:.1f} ({rr}{lal}) · отмена {word} {pl.cancel:.1f}{wide}"


def render_text(r: AnalysisResult, max_figs: int = 2) -> str:
    """Ответ «дай зону» по формату раздела 12 ТЗ."""
    b = r.bias
    lines = [f"Золото {r.price:.2f} · Bias: D1 {b.get('d1', '—')}, H4 {b.get('h4', '—')} · сессия {r.session}", ""]
    if not r.zones:
        lines.append("Зон по правилам стратегии сейчас нет: нет подтверждённых манипуляций рядом с ценой.")
    for z in r.zones:
        icon = "🟢" if z.dir == "BUY" else "🔴"
        lines.append(f"{icon} {z.dir} {z.lo:.1f}–{z.hi:.1f} · {z.stars} · {z.type} · {z.status}")
        info = ", ".join(z.srcs[:4])
        if z.notes:
            info += ", " + "; ".join(z.notes[:2])
        lines.append(f"   {info}")
        if z.plan:
            lines.append("   " + _entry_line(z))
        lines.append("")
    for f in r.figures[:max_figs]:
        lines.append(f"Книга: {f.text}")
    if r.candle_models:
        lines.append("Свечи книги: " + ", ".join(r.candle_models[:3]) + " (карта стопов, не вход)")
    for w in r.warnings:
        lines.append(f"⚠️ {w}")
    for d in r.robot_diff:
        lines.append(f"⚠️ {d}")
    lines.append("Решение за тобой, это анализ, не гарантия.")
    return "\n".join(lines).strip()
