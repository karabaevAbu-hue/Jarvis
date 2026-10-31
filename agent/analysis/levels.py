"""Bias (D1/H4), HTF Re-test Levels (LAL Levels), тип зоны WITH/MIXED/COUNTER, сессия."""
from __future__ import annotations

from datetime import datetime

import numpy as np

from agent.analysis import params as P
from agent.analysis.liquidity import swings
from agent.analysis.models import RetestLevel
from agent.analysis.patterns import Candles, doji_level, fu_valid, find_fus, find_hcs, find_negations


def calc_bias(cs: Candles, lookback: int = 40) -> tuple[str, str]:
    """Направление потока на ТФ по последней значимой манипуляции.

    Значимые: FU классов A/B, Negation, HCS. Берём самую свежую (FU учитывается, только пока жив).
    Если их нет — структура swing-точек (HH+HL / LH+LL). Возвращает (BUY|SELL|NEUTRAL, причина).
    """
    if cs.n < 10:
        return "NEUTRAL", "мало данных"
    start = cs.n - lookback
    fus = find_fus(cs, start=max(1, start))
    events: list[tuple[int, int, str, str]] = []   # (idx, сила, направление, описание)
    for f in fus:
        if f.cls in ("A", "B") and fu_valid(cs, f):
            events.append((f.idx, 1 if f.cls == "B" else 2, f.dir, f"{f.kind} класс {f.cls}"))
    for n in find_negations(cs, find_fus(cs, start=max(1, start - 2))):
        if n.idx >= start:
            events.append((n.idx, 3 if n.strong or n.count == 3 else 2, n.dir,
                           "Negation x3" if n.count == 3 else "Negation"))
    for h in find_hcs(cs, find_fus(cs, start=max(1, start - 60))):
        if h.retest.idx >= start:
            events.append((h.retest.idx, 3 + h.count, h.dir, f"HCS x{h.count}" if h.count > 1 else "HCS"))
    if events:
        events.sort(key=lambda e: (e[0], e[1]))
        idx, _, d, why = events[-1]
        return d, f"{why} {cs.t[idx]:%d.%m %H:%M}"
    hi, lo = swings(cs)
    if len(hi) >= 2 and len(lo) >= 2:
        if cs.h[hi[-1]] > cs.h[hi[-2]] and cs.l[lo[-1]] > cs.l[lo[-2]]:
            return "BUY", "HH+HL"
        if cs.h[hi[-1]] < cs.h[hi[-2]] and cs.l[lo[-1]] < cs.l[lo[-2]]:
            return "SELL", "LH+LL"
    return "NEUTRAL", "нет явной манипуляции"


def zone_type(zone_dir: str, d1: str, h4: str) -> str:
    """WITH — зона по потоку обоих ТФ, COUNTER — против обоих, иначе MIXED."""
    same = [b == zone_dir for b in (d1, h4) if b in ("BUY", "SELL")]
    against = [b != zone_dir for b in (d1, h4) if b in ("BUY", "SELL")]
    if same and all(same) and len(same) == 2:
        return "WITH"
    if against and all(against) and len(against) == 2:
        return "COUNTER"
    return "MIXED"


def htf_retest_levels(cs: Candles, price: float, lookback: int = 80) -> list[RetestLevel]:
    """Пара «Doji + следующая большая свеча с тенью» старшего ТФ и уровни 1-4.

    SELL-сценарий (цена подходит снизу вверх): L1 лой Doji, L2 50% тела Doji, L3 хай Doji,
    L4 50% верхней тени большой свечи. BUY-сценарий — зеркально (порядок уровней для BUY
    на слайде не подтверждён, см. раздел 12 стратегии). Сценарий отменён, если уровень 4 пробит
    закрытием или цена уже за ним.
    """
    atr = cs.atr()
    out: dict[str, RetestLevel] = {}
    res: list[RetestLevel] = []
    for i in range(max(1, cs.n - lookback), cs.n - 1):
        if doji_level(cs, i) == 0:
            continue
        j = i + 1
        rng = cs.rng(j)
        if rng < 1.2 * atr:
            continue
        up = (cs.h[j] - max(cs.o[j], cs.c[j])) / rng
        lo = (min(cs.o[j], cs.c[j]) - cs.l[j]) / rng
        mid_body = (cs.o[i] + cs.c[i]) / 2
        if up >= P.WICK_RATIO:
            l4 = (cs.h[j] + max(cs.o[j], cs.c[j])) / 2
            if price < l4 and not np.any(cs.c[j:] > l4):
                out["SELL"] = RetestLevel(0, 0, "SELL", cs.tf)
                res = [r for r in res if r.dir != "SELL"]
                for n, p, lab in ((1, cs.l[i], "лой Doji"), (2, mid_body, "50% Doji"),
                                  (3, cs.h[i], "хай Doji"), (4, l4, "50% тени")):
                    res.append(RetestLevel(n, float(p), "SELL", cs.tf, lab))
        if lo >= P.WICK_RATIO:
            l4 = (cs.l[j] + min(cs.o[j], cs.c[j])) / 2
            if price > l4 and not np.any(cs.c[j:] < l4):
                res = [r for r in res if r.dir != "BUY"]
                for n, p, lab in ((1, cs.h[i], "хай Doji"), (2, mid_body, "50% Doji"),
                                  (3, cs.l[i], "лой Doji"), (4, l4, "50% тени")):
                    res.append(RetestLevel(n, float(p), "BUY", cs.tf, lab))
    return sorted(res, key=lambda r: (r.dir, r.n))


def session_name(dt_utc: datetime) -> str:
    h = dt_utc.hour
    for name, a, b in P.SESSIONS_UTC:
        if a <= h < b:
            return name
    return "OFF"
