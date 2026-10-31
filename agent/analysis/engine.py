"""Оркестратор анализа «дай зону»: данные -> bias -> зоны робота -> план -> ликвидность и книга -> текст.

Зоны, bias и HTF-уровни считает порт робота (`robot_port.RobotZones`), поэтому они совпадают с зонами
робота. Поверх идёт слой, которого у робота нет: ликвидность (major/minor, трендовые), имбалансы по ATR,
фигуры книги как карта стопов, свечные модели. Цифры зон берутся только отсюда: ИИ границы не придумывает.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from agent.analysis import figures as figmod
from agent.analysis import liquidity as liqmod
from agent.analysis import params as P
from agent.analysis.data_mt5 import CandleSource, MT5Unavailable, load_frames
from agent.analysis.levels import session_name
from agent.analysis.liquidity import nearest_major
from agent.analysis.models import AnalysisResult, Liquidity, Plan, RetestLevel, Zone
from agent.analysis.patterns import Candles, candle_models
from agent.analysis.robot_port import STATUS_NAME, TYPE_NAME, RobotParams, RobotZones, RZone, Ser

BIAS_NAME = {1: "BUY", -1: "SELL", 0: "NEUTRAL"}
SER_TFS = ("M5", "M15", "H1", "H4", "D1")


def make_plan(rz: RobotZones, z: Zone, rzone: RZone, liq: list[Liquidity], spread: float = 0.2) -> Plan:
    """План по правилам TryEntry (True Stop, стоп 5..15, тейк стоп x RR) + ближайшая major ликвидность как LAL."""
    pl = rz.plan(rzone, spread)
    lal = nearest_major(liq, z.dir, pl["entry"])
    plan = Plan(pl["entry"], pl["sl"], pl["tp"], round(lal.price, 2) if lal else None, pl["risk_pts"], pl["cancel"],
                pl["too_wide"])
    z.plan = plan
    return plan


class AnalysisEngine:
    def __init__(self, source: CandleSource, symbol: str = "", params: RobotParams = RobotParams()):
        self.source = source
        self.symbol = symbol
        self.params = params
        self.last_frames: dict[str, Candles] = {}
        self.last_robot: RobotZones | None = None

    # ------------------------------------------------------------------ основной вход
    def analyze(self, tf: str | None = None, side: str | None = None, now: datetime | None = None) -> AnalysisResult:
        symbol = self.source.find_symbol(self.symbol)
        if not symbol:
            raise MT5Unavailable("символ золота не найден в MT5")
        raw = load_frames(self.source, symbol)
        tick = self.source.tick(symbol)
        return self.analyze_frames(raw, symbol, tick, tf, side, now)

    def analyze_frames(self, raw: dict[str, pd.DataFrame], symbol: str, tick: dict | None = None,
                       tf: str | None = None, side: str | None = None, now: datetime | None = None,
                       spread: float | None = None) -> AnalysisResult:
        """Анализ на готовом срезе свечей (живой MT5, симулятор или выгрузка для сверки с роботом)."""
        warnings: list[str] = []
        for need in ("D1", "H4", "H1", "M15", "M5"):
            if need not in raw or len(raw[need]) < 20:
                warnings.append(f"нет данных {need}: анализ неполный")
        if "H1" not in raw or "M5" not in raw:
            raise MT5Unavailable("нет свечей H1 или M5")
        frames = {k: Candles(v, k) for k, v in raw.items() if len(v) >= 20}
        price = float(tick["bid"]) if tick else float(frames["M5"].c[-1])
        if spread is None:
            spread = float(tick["ask"] - tick["bid"]) if tick and "ask" in tick else 0.2
        # «сейчас»: время последнего тика, иначе последняя свеча M5 (так зоны не протухают на архивных данных)
        when = now or (tick["time"] if tick and tick.get("time") is not None else None)
        if when is None:
            when = frames["M5"].t[-1].to_pydatetime() + pd.Timedelta(seconds=150)
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)

        rz = RobotZones({k: Ser.from_df(raw[k], k) for k in SER_TFS if k in raw}, price, int(when.timestamp()), self.params)
        rzones = rz.build()
        self.last_robot = rz
        bias = {"d1": BIAS_NAME[rz.bias["D1"]], "h4": BIAS_NAME[rz.bias["H4"]]}

        liq = liqmod.collect(frames)
        imbs = []
        for name in ("H4", "H1"):
            if name in frames:
                imbs += liqmod.imbalances(frames[name])
        levels = self._levels(rz)

        atr_h1 = frames["H1"].atr() if "H1" in frames else 0.0
        zones: list[Zone] = []
        robot_zones: list[dict] = []
        for z in rzones:
            if not z.tradable:
                continue
            zone = self._to_zone(rz, z)
            make_plan(rz, zone, z, liq, spread)
            zones.append(zone)
            robot_zones.append({"dir": zone.dir, "lo": z.lo, "hi": z.hi, "status": zone.status, "score": z.score,
                                "srcs": z.srcs, "htf": z.htf, "dist": zone.dist})
        shown = []
        for d in ("BUY", "SELL"):
            mine = [z for z in zones if z.dir == d and (side is None or side == d)]
            if tf in ("M15", "H1", "H4"):
                mine = [z for z in mine if any(s.startswith(tf) for s in z.srcs)]
            mine.sort(key=lambda z: (-z.score, z.dist))
            shown += mine[:2]

        figs = []
        for name in ("H1", "H4"):
            if name in frames:
                figs += figmod.detect(frames[name])
        models = []
        for name in ("M15", "H1"):
            if name in frames:
                models += [f"{name} {m}" for m in candle_models(frames[name])]
        self.last_frames = frames
        return AnalysisResult(symbol=symbol, price=price, bias=bias, zones=shown, liquidity=liq, levels=levels,
                              figures=figs, imbalances=imbs, candle_models=models, session=session_name(when),
                              tf=tf or "H1", frames_ok={k: v.n for k, v in frames.items()}, warnings=warnings,
                              robot_zones=robot_zones)

    # ------------------------------------------------------------------ преобразования
    @staticmethod
    def _to_zone(rz: RobotZones, z: RZone) -> Zone:
        notes: list[str] = []
        ty = TYPE_NAME[rz.trade_type(z.dir)]
        if rz.zone_in_fvg(z.lo, z.hi):
            notes.append("зона внутри имбаланса (−2 к баллу), разворот из имбаланса не берём")
        if ty == "COUNTER":
            notes.append("против потока D1 и H4: нужен HCS, Negation или FU класса A")
        if z.htf:
            notes.append("совпала с HTF Re-test Level H4")
        return Zone(dir=z.dir_name, lo=z.lo, hi=z.hi, status=STATUS_NAME[z.status], score=z.score, type=ty,
                    srcs=z.srcs.split(" + ") if z.srcs else [], htf=z.htf, dist=round(rz.zone_dist(z), 2), notes=notes)

    @staticmethod
    def _levels(rz: RobotZones) -> list[RetestLevel]:
        out: list[RetestLevel] = []
        names = {"BUY": ["хай Doji", "50% Doji", "лой Doji", "50% тени"], "SELL": ["лой Doji", "50% Doji", "хай Doji", "50% тени"]}
        for d, lv in rz.lv.items():
            if lv is None:
                continue
            direction = "BUY" if d > 0 else "SELL"
            for n, price in enumerate(lv, 1):
                out.append(RetestLevel(n, round(price, 2), direction, "H4", names[direction][n - 1]))
        return sorted(out, key=lambda r: (r.dir, r.n))


# ------------------------------------------------------------------ сверка с роботом
def compare_with_robot(result: AnalysisResult, robot_zones: list[dict], tol: float = 1.0) -> list[str]:
    """Расхождение торгуемых зон порта и зон из status.json робота больше tol пунктов (раздел 12.7 ТЗ).

    Сравниваются множества торгуемых зон: у робота их до 3 на сторону, и это именно зоны status.json.
    """
    out: list[str] = []
    mine = result.robot_zones
    for rz in robot_zones:
        d = rz.get("dir")
        cand = [m for m in mine if m["dir"] == d and m["lo"] <= rz["hi"] + tol and m["hi"] >= rz["lo"] - tol]
        if not cand:
            out.append(f"Робот видит {d} {rz['lo']:.2f}–{rz['hi']:.2f}, движок такую зону не построил "
                       f"(нет таких источников на этом срезе свечей или зона дальше радиуса)")
            continue
        m = min(cand, key=lambda q: abs(q["lo"] - rz["lo"]) + abs(q["hi"] - rz["hi"]))
        dlo, dhi = abs(m["lo"] - rz["lo"]), abs(m["hi"] - rz["hi"])
        if max(dlo, dhi) > tol:
            out.append(f"{d}: робот {rz['lo']:.2f}–{rz['hi']:.2f}, движок {m['lo']:.2f}–{m['hi']:.2f} "
                       f"(расхождение границ {max(dlo, dhi):.2f} pt)")
        elif rz.get("status") and rz["status"] != m["status"]:
            out.append(f"{d} {rz['lo']:.2f}–{rz['hi']:.2f}: статус робота {rz['status']}, движка {m['status']}")
    for m in mine:
        if not any(rz.get("dir") == m["dir"] and m["lo"] <= rz["hi"] + tol and m["hi"] >= rz["lo"] - tol
                   for rz in robot_zones):
            out.append(f"Движок держит {m['dir']} {m['lo']:.2f}–{m['hi']:.2f}, а робот эту зону в status.json не отдаёт")
    return out


# ------------------------------------------------------------------ текст
def _entry_line(z: Zone) -> str:
    pl = z.plan
    assert pl is not None
    if z.status == "CONFIRMED":
        how = "Вход: после FU / HCS / Negation / снятия ликвидности в зоне"
    else:
        how = "Зона NEW: вход только после подтверждения (отход и возврат), затем FU / HCS / Negation"
    rr = f"1:{pl.risk_pts and round(abs(pl.tp - pl.entry_ref) / pl.risk_pts):g}"
    lal = f", LAL {pl.tp_lal:g}" if pl.tp_lal else ""
    word = "ниже" if z.dir == "BUY" else "выше"
    wide = f" ⚠️ стоп {pl.risk_pts:g} > {P.SL_MAX:g}, робот такой вход пропустит" if pl.stop_too_wide else ""
    return f"{how} · SL {pl.sl:.1f} · TP {pl.tp:.1f} ({rr}{lal}) · отмена {word} {pl.cancel:.1f}{wide}"


def render_text(r: AnalysisResult, max_figs: int = 2) -> str:
    """Ответ «дай зону» по формату раздела 12 ТЗ."""
    b = r.bias
    lines = [f"Золото {r.price:.2f} · Bias: D1 {b.get('d1', '—')}, H4 {b.get('h4', '—')} · сессия {r.session}", ""]
    if not r.zones:
        lines.append("Зон по правилам робота сейчас нет: рядом с ценой нет подходящих источников (OB, FU, ликвидность).")
    for z in r.zones:
        icon = "🟢" if z.dir == "BUY" else "🔴"
        lines.append(f"{icon} {z.dir} {z.lo:.1f}–{z.hi:.1f} · {z.stars} · {z.type} · {z.status} · балл {z.score}")
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
