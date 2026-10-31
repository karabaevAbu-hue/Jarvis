"""Ликвидность: равные хаи/лои, Doji, большие тени, трендовые линии 3+, имбалансы (FVG).

Major / minor — по таблице «Расчёт ликвидности» раздела 9 стратегии:
  major: Doji (не AFU), идеальные двойные вершины/основания, трендовые 3+ касаний, имбалансные свечи;
  minor: AFU, «грязные» уровни, линии с <3 касаний, сбалансированные свечи.
Уровень, который цена уже забрала (пробила), из списка выпадает.
"""
from __future__ import annotations

import numpy as np

from agent.analysis import params as P
from agent.analysis.models import FU, Imbalance, Liquidity
from agent.analysis.patterns import Candles, find_dojis, find_fus


def swings(cs: Candles, left: int = 2, right: int = 2) -> tuple[list[int], list[int]]:
    """Индексы swing-хаёв и swing-лоёв (фракталы)."""
    hi, lo = [], []
    for i in range(left, cs.n - right):
        if cs.h[i] > cs.h[i - left:i].max() and cs.h[i] >= cs.h[i + 1:i + right + 1].max():
            hi.append(i)
        if cs.l[i] < cs.l[i - left:i].min() and cs.l[i] <= cs.l[i + 1:i + right + 1].min():
            lo.append(i)
    return hi, lo


def _group(idxs: list[int], vals: np.ndarray, tol: float, min_sep: int) -> list[list[int]]:
    """Группы swing-точек с близкими значениями, между соседними не меньше min_sep свечей."""
    groups: list[list[int]] = []
    for i in idxs:
        placed = False
        for g in groups:
            if abs(vals[i] - np.mean([vals[k] for k in g])) <= tol and i - g[-1] >= min_sep:
                g.append(i)
                placed = True
                break
        if not placed:
            groups.append([i])
    return [g for g in groups if len(g) >= 2]


def equal_levels(cs: Candles, lookback: int = 150) -> list[Liquidity]:
    """Двойные/тройные вершины и основания: ровные (major) и грязные (minor)."""
    start = max(0, cs.n - lookback)
    atr = cs.atr()
    hi_i, lo_i = swings(cs)
    out: list[Liquidity] = []
    for side, idxs, vals in (("HIGH", [i for i in hi_i if i >= start], cs.h),
                             ("LOW", [i for i in lo_i if i >= start], cs.l)):
        for tol, major in ((max(P.EQ_TOL_ATR * atr, 0.05), True), (P.DIRTY_TOL_ATR * atr, False)):
            for g in _group(idxs, vals, tol, 3):
                price = float(max(vals[k] for k in g)) if side == "HIGH" else float(min(vals[k] for k in g))
                last = g[-1]
                swept = (np.any(cs.h[last + 1:] > price + 1e-9) if side == "HIGH"
                         else np.any(cs.l[last + 1:] < price - 1e-9))
                if swept:
                    continue
                if not major and any(abs(o.price - price) <= tol and o.side == side and o.major for o in out):
                    continue
                kind = ("EQ_HIGH" if side == "HIGH" else "EQ_LOW") if major else \
                       ("DIRTY_HIGH" if side == "HIGH" else "DIRTY_LOW")
                n = len(g)
                note = ("тройная вершина" if side == "HIGH" else "тройное дно") if n >= 3 else \
                       ("ровные хаи" if side == "HIGH" else "ровные лои")
                if not major:
                    note = "грязные хаи" if side == "HIGH" else "грязные лои"
                out.append(Liquidity(kind, side, price, major, cs.tf, note, touches=n))
    return out


def doji_levels(cs: Candles, fus: list[FU] | None = None, lookback: int = 80) -> list[Liquidity]:
    """Хай и лой каждого непробитого Doji = major; последние AFU (не Doji) = minor."""
    fus = fus if fus is not None else find_fus(cs)
    out: list[Liquidity] = []
    start = max(1, cs.n - lookback)
    for i in find_dojis(cs, fus):
        if i < start or i >= cs.n - 1:
            continue
        if not np.any(cs.h[i + 1:] > cs.h[i]):
            out.append(Liquidity("DOJI", "HIGH", float(cs.h[i]), True, cs.tf, "хай Doji"))
        if not np.any(cs.l[i + 1:] < cs.l[i]):
            out.append(Liquidity("DOJI", "LOW", float(cs.l[i]), True, cs.tf, "лой Doji"))
    for f in fus:
        if f.idx < cs.n - 30 or f.kind != "AFU":
            continue
        # AFU сам по себе — просто манипуляция, цель будет дальше (minor)
        if f.dir == "BUY" and not np.any(cs.l[f.idx + 1:] < f.wick_lo):
            out.append(Liquidity("AFU", "LOW", f.wick_lo, False, cs.tf, "лой AFU"))
        if f.dir == "SELL" and not np.any(cs.h[f.idx + 1:] > f.wick_hi):
            out.append(Liquidity("AFU", "HIGH", f.wick_hi, False, cs.tf, "хай AFU"))
    return out


def big_wicks(cs: Candles, lookback: int = 60) -> list[Liquidity]:
    """Длинные тени: свеча >= 1 ATR, тень >= 50% диапазона."""
    atr = cs.atr()
    out: list[Liquidity] = []
    for i in range(max(0, cs.n - lookback), cs.n - 1):
        rng = cs.rng(i)
        if rng < P.BIG_WICK_ATR * atr:
            continue
        up = cs.h[i] - max(cs.o[i], cs.c[i])
        lo = min(cs.o[i], cs.c[i]) - cs.l[i]
        if up / rng >= 0.5 and not np.any(cs.h[i + 1:] > cs.h[i]):
            out.append(Liquidity("WICK", "HIGH", float(cs.h[i]), True, cs.tf, "большая тень вверх"))
        if lo / rng >= 0.5 and not np.any(cs.l[i + 1:] < cs.l[i]):
            out.append(Liquidity("WICK", "LOW", float(cs.l[i]), True, cs.tf, "большая тень вниз"))
    return out


def trendlines(cs: Candles, lookback: int = 150) -> list[Liquidity]:
    """Идеальные трендовые линии: >= 3 касаний swing-точек, закрытия линию не пробивали.

    Линии с меньшим числом касаний — minor и не возвращаются (любые две точки задают линию).
    """
    start = max(0, cs.n - lookback)
    atr = cs.atr()
    tol = P.TREND_TOL_ATR * atr
    hi_i, lo_i = swings(cs)
    out: list[Liquidity] = []
    for side, idxs, vals in (("LOW", [i for i in lo_i if i >= start][-8:], cs.l),
                             ("HIGH", [i for i in hi_i if i >= start][-8:], cs.h)):
        best = None
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                x1, x2 = idxs[a], idxs[b]
                if x2 - x1 < 4:
                    continue
                slope = (vals[x2] - vals[x1]) / (x2 - x1)
                line = lambda x: vals[x1] + slope * (x - x1)  # noqa: E731
                touches = [k for k in idxs if k >= x1 and abs(vals[k] - line(k)) <= tol]
                if len(touches) < P.TREND_MIN_TOUCHES:
                    continue
                span = np.arange(x1, cs.n)
                ln = vals[x1] + slope * (span - x1)
                broken = np.any(cs.c[x1:] < ln - tol) if side == "LOW" else np.any(cs.c[x1:] > ln + tol)
                if broken:
                    continue
                score = (len(touches), touches[-1])
                if best is None or score > best[0]:
                    best = (score, float(line(cs.n - 1)), len(touches))
        if best:
            out.append(Liquidity("TREND", side, best[1], True, cs.tf,
                                 f"трендовая линия, {best[2]} касания", touches=best[2]))
    return out


def imbalances(cs: Candles, lookback: int = 120) -> list[Imbalance]:
    """Незакрытые FVG (разрыв между свечой i-1 и i+1). Размер >= FVG_MIN_ATR * ATR."""
    atr = cs.atr()
    out: list[Imbalance] = []
    for i in range(max(1, cs.n - lookback), cs.n - 1):
        if cs.l[i + 1] > cs.h[i - 1]:                                 # бычий разрыв
            lo, hi = float(cs.h[i - 1]), float(cs.l[i + 1])
            if hi - lo < P.FVG_MIN_ATR * atr:
                continue
            later = cs.l[i + 2:]
            if later.size:
                m = float(later.min())
                if m <= lo:
                    continue
                hi = min(hi, m)
            out.append(Imbalance(cs.tf, "BUY", lo, hi, i, cs.t[i]))
        elif cs.h[i + 1] < cs.l[i - 1]:                               # медвежий разрыв
            lo, hi = float(cs.h[i + 1]), float(cs.l[i - 1])
            if hi - lo < P.FVG_MIN_ATR * atr:
                continue
            later = cs.h[i + 2:]
            if later.size:
                m = float(later.max())
                if m >= hi:
                    continue
                lo = max(lo, m)
            out.append(Imbalance(cs.tf, "SELL", lo, hi, i, cs.t[i]))
    return out


def collect(frames: dict[str, Candles], tfs: tuple[str, ...] = ("M15", "H1", "H4", "D1")) -> list[Liquidity]:
    """Вся ликвидность по таймфреймам. Дубли (одинаковая цена и сторона) схлопываются."""
    out: list[Liquidity] = []
    for tf in tfs:
        cs = frames.get(tf)
        if cs is None or cs.n < 20:
            continue
        fus = find_fus(cs)
        out += equal_levels(cs) + doji_levels(cs, fus) + big_wicks(cs) + trendlines(cs)
    uniq: dict[tuple[str, float], Liquidity] = {}
    for q in out:
        key = (q.side, round(q.price, 1))
        if key not in uniq or (q.major and not uniq[key].major):
            uniq[key] = q
    return sorted(uniq.values(), key=lambda q: q.price)


def nearest_major(liq: list[Liquidity], direction: str, from_price: float) -> Liquidity | None:
    """LAL / цель: ближайшая major ликвидность по ходу сделки.

    BUY — цель сверху (стопы над уровнем, side=HIGH); SELL — снизу (side=LOW).
    """
    if direction == "BUY":
        c = [q for q in liq if q.major and q.side == "HIGH" and q.price > from_price]
        return min(c, key=lambda q: q.price) if c else None
    c = [q for q in liq if q.major and q.side == "LOW" and q.price < from_price]
    return max(c, key=lambda q: q.price) if c else None
