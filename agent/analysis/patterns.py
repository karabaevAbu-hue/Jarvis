"""Паттерны Mr. Casino и свечные модели книги: Doji, FU и классы, Negation, HCS, re-test.

Все функции работают на массивах. Для SELL используется зеркало цен (o,h,l,c) -> (-o,-l,-h,-c):
медвежий FU в зеркале становится бычьим, поэтому логика классификации написана один раз для BUY.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from agent.analysis import params as P
from agent.analysis.models import FU, HCS, OB, Negation, Retest


class Candles:
    """Обёртка над DataFrame со столбцами open/high/low/close и временным индексом."""

    def __init__(self, df: pd.DataFrame, tf: str = ""):
        self.df = df
        self.tf = tf
        self.o = df["open"].to_numpy(float)
        self.h = df["high"].to_numpy(float)
        self.l = df["low"].to_numpy(float)
        self.c = df["close"].to_numpy(float)
        self.t = df.index
        self.n = len(df)

    def atr(self, period: int = 14, upto: int | None = None) -> float:
        end = self.n if upto is None else upto + 1
        if end < 2:
            return float(self.h[0] - self.l[0]) if self.n else 0.0
        h, l, c = self.h[:end], self.l[:end], self.c[:end]
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
        return float(tr[-period:].mean())

    def rng(self, i: int) -> float:
        return float(self.h[i] - self.l[i])


def from_rows(rows: list[tuple[float, float, float, float]], tf: str = "H1", start: str = "2026-01-05") -> Candles:
    """Для тестов: список (o,h,l,c) -> Candles с часовым индексом."""
    idx = pd.date_range(start, periods=len(rows), freq=pd.Timedelta(hours=1) if tf != "M15" else "15min", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    return Candles(df, tf)


# ---------------------------------------------------------------- FU
def _fu_buy(o, h, l, c, po, ph, pl, pc) -> int | None:
    """Бычий FU относительно предыдущей свечи. Возвращает тип 1..7 (0 — без признаков) или None.

    Допущения по таблице раздела 4 стратегии:
      grab     — low ниже low предыдущей, close выше него (ликвидность снята и возвращена);
      BIW      — тело целиком внутри нижней тени предыдущей свечи;
      engulf   — тело перекрывает тело предыдущей;
      covered  — close зашёл в тело предыдущей свечи или выше;
      wick     — нижняя тень >= WICK_RATIO диапазона;
      reversed — бычья свеча после медвежьей.
    """
    if not (l < pl and c > pl):
        return None
    rng = h - l
    if rng <= 0:
        return None
    body_lo, body_hi = min(o, c), max(o, c)
    p_lo, p_hi = min(po, pc), max(po, pc)
    bullish = c > o
    wick = (body_lo - l) / rng >= P.WICK_RATIO
    biw = body_hi <= p_lo and body_lo >= pl
    engulf = bullish and c >= p_hi and o <= p_lo
    covered = bullish and c > p_lo
    reversed_ = bullish and pc < po
    if biw and bullish and wick:
        return 1
    if engulf:
        return 2
    if covered and wick:
        return 3
    if covered:
        return 4
    if wick:
        return 5
    if reversed_:
        return 6
    if biw:
        return 7
    return 0


def _classify(t: int) -> tuple[str, str]:
    """тип -> (SFU/AFU, класс A/B/C). Типы 1-3 — самые качественные (раздел 4)."""
    kind = "SFU" if t in (1, 2) else "AFU"
    cls = "A" if t in (1, 2, 3) else "B" if t in (4, 5) else "C"
    return kind, cls


def fu_at(cs: Candles, i: int) -> list[FU]:
    """FU на свече i (обычно один; у внешней свечи может быть оба направления)."""
    if i < 1 or i >= cs.n:
        return []
    out: list[FU] = []
    o, h, l, c = cs.o[i], cs.h[i], cs.l[i], cs.c[i]
    po, ph, pl, pc = cs.o[i - 1], cs.h[i - 1], cs.l[i - 1], cs.c[i - 1]
    t = _fu_buy(o, h, l, c, po, ph, pl, pc)
    if t is not None:
        kind, cls = _classify(t)
        out.append(FU(i, cs.t[i], "BUY", t, kind, cls, float(pl), float(l), float(min(o, c)), cs.tf))
    # зеркало для SELL: (o,h,l,c) -> (-o,-l,-h,-c)
    t = _fu_buy(-o, -l, -h, -c, -po, -pl, -ph, -pc)
    if t is not None:
        kind, cls = _classify(t)
        out.append(FU(i, cs.t[i], "SELL", t, kind, cls, float(ph), float(max(o, c)), float(h), cs.tf))
    return out


def find_fus(cs: Candles, start: int = 1) -> list[FU]:
    res: list[FU] = []
    for i in range(max(1, start), cs.n):
        res.extend(fu_at(cs, i))
    return res


def fu_valid(cs: Candles, fu: FU) -> bool:
    """FU жив, пока ни одна свеча после него не закрылась за дальним краем тени."""
    after = slice(fu.idx + 1, cs.n)
    if fu.dir == "BUY":
        return not bool(np.any(cs.c[after] < fu.wick_lo))
    return not bool(np.any(cs.c[after] > fu.wick_hi))


# ---------------------------------------------------------------- Negation
def _failed_pierce_buy(cs: Candles, i: int) -> bool:
    """Свеча i пробила low предыдущей и закрылась ниже него (FU, который «не вернулся»)."""
    return i >= 1 and cs.l[i] < cs.l[i - 1] and cs.c[i] < cs.l[i - 1]


def _failed_pierce_sell(cs: Candles, i: int) -> bool:
    return i >= 1 and cs.h[i] > cs.h[i - 1] and cs.c[i] > cs.h[i - 1]


def find_negations(cs: Candles, fus: list[FU] | None = None) -> list[Negation]:
    """Negation: FU, который следующая же свеча отрицает своим FU в обратную сторону.

    Первая свеча — либо сам FU, либо «проваленный» прокол (закрылась за уровнем).
    Сильный вариант: у первой свечи почти нет тени, вторая её поглощает и оставляет большую тень.
    Если отрицание само отрицается следующей свечой — count=3 (x3).
    """
    fus = fus if fus is not None else find_fus(cs)
    by_idx: dict[int, list[FU]] = {}
    for f in fus:
        by_idx.setdefault(f.idx, []).append(f)
    found: dict[int, Negation] = {}
    for j in range(2, cs.n):
        for f in by_idx.get(j, []):
            prev_fus = [p for p in by_idx.get(j - 1, []) if p.dir != f.dir]
            if f.dir == "BUY":
                failed = _failed_pierce_buy(cs, j - 1)
            else:
                failed = _failed_pierce_sell(cs, j - 1)
            if not prev_fus and not failed:
                continue
            i = j - 1
            rng_i = max(cs.rng(i), 1e-9)
            rng_j = max(cs.rng(j), 1e-9)
            if f.dir == "BUY":
                tail_i = (min(cs.o[i], cs.c[i]) - cs.l[i]) / rng_i
                tail_j = (min(cs.o[j], cs.c[j]) - cs.l[j]) / rng_j
                engulf = cs.c[j] >= cs.o[i] and cs.o[j] <= cs.c[i] and cs.c[j] > cs.o[j]
            else:
                tail_i = (cs.h[i] - max(cs.o[i], cs.c[i])) / rng_i
                tail_j = (cs.h[j] - max(cs.o[j], cs.c[j])) / rng_j
                engulf = cs.c[j] <= cs.o[i] and cs.o[j] >= cs.c[i] and cs.c[j] < cs.o[j]
            strong = failed and tail_i <= P.STRONG_NEG_MAX_WICK and engulf and tail_j >= P.WICK_RATIO
            n = Negation(j, cs.t[j], f.dir, bool(strong), 2, cs.tf)
            # цепочка: предыдущая негация в обратную сторону на j-1 -> x3
            prev = found.get(j - 1)
            if prev is not None and prev.dir != f.dir:
                n.count = 3
                n.strong = n.strong or prev.strong
            found[j] = n
    return [found[k] for k in sorted(found)]


# ---------------------------------------------------------------- re-test и HCS
def _mirror_lookup(cs: Candles, dir_: str):
    """Возвращает (lows, highs, closes, opens) в системе, где направление = BUY."""
    if dir_ == "BUY":
        return cs.l, cs.h, cs.c, cs.o
    return -cs.h, -cs.l, -cs.c, -cs.o


def _zone_m(fu: FU) -> tuple[float, float]:
    """Зона тени FU в системе BUY."""
    return (fu.wick_lo, fu.wick_hi) if fu.dir == "BUY" else (-fu.wick_hi, -fu.wick_lo)


def find_hcs(cs: Candles, fus: list[FU], max_gap: int = 60,
             htf_obs: list[OB] | None = None) -> list[HCS]:
    """HCS = FU, перетестированный другим FU (тоже FU) в зоне тени первого.

    Условия для пары (a, b): b позже a; цена ушла от зоны a и вернулась; зона a не пробита
    закрытием; low (для BUY) свечи b лежит внутри тени a. Цепочки дают x2 и x3.
    """
    out: list[HCS] = []
    tol = 0.05 * cs.atr()
    for d in ("BUY", "SELL"):
        lows, highs, closes, _ = _mirror_lookup(cs, d)
        fl = sorted((f for f in fus if f.dir == d), key=lambda f: f.idx)
        chain: dict[int, int] = {}
        best: dict[int, tuple[FU, int]] = {}
        for bi, b in enumerate(fl):
            cnt, first = 0, None
            for a in fl[:bi]:
                gap = b.idx - a.idx
                if gap < 2 or gap > max_gap:
                    continue
                zlo, zhi = _zone_m(a)
                b_ext = lows[b.idx]
                if not (zlo - tol <= b_ext <= zhi + tol):
                    continue
                mid = slice(a.idx + 1, b.idx)
                if np.any(closes[mid] < zlo):          # зона пробита закрытием
                    continue
                if not np.any(lows[mid] > zhi):        # цена не уходила от зоны
                    continue
                c = chain.get(id(a), 0) + 1
                if c > cnt:
                    cnt, first = c, a
            if first is not None:
                chain[id(b)] = cnt
                best[b.idx] = (first, min(cnt, 3))
                out.append(HCS(first=first, retest=b, count=min(cnt, 3), tf=cs.tf))
    if htf_obs:
        for h in out:
            h.in_htf_ob = in_ob(h.first.wick_lo, h.first.wick_hi, h.dir, htf_obs)
    return sorted(out, key=lambda h: h.retest.idx)


def in_ob(lo: float, hi: float, dir_: str, obs: list[OB]) -> bool:
    """Зона лежит внутри (пересекается с) OB старшего ТФ в том же направлении."""
    return any(o.dir == dir_ and lo <= o.hi and hi >= o.lo for o in obs)


def find_retests(cs: Candles, fus: list[FU], max_gap: int = 60) -> list[Retest]:
    """FU re-test: цена вернулась в зону тени FU и отбилась обычной свечой с тенью."""
    out: list[Retest] = []
    fu_idx = {(f.idx, f.dir) for f in fus}
    tol = 0.05 * cs.atr()
    for a in fus:
        lows, highs, closes, opens = _mirror_lookup(cs, a.dir)
        zlo, zhi = _zone_m(a)
        left = False
        for k in range(a.idx + 1, min(cs.n, a.idx + 1 + max_gap)):
            if closes[k] < zlo:
                break                                   # зона пробита
            if lows[k] > zhi:
                left = True
                continue
            if left and zlo - tol <= lows[k] <= zhi + tol and closes[k] > zhi and (k, a.dir) not in fu_idx:
                rng = highs[k] - lows[k]
                wick = (min(opens[k], closes[k]) - lows[k]) / rng if rng > 0 else 0
                if wick >= 0.3:
                    out.append(Retest(a, k, cs.t[k]))
                    break
    return out


# ---------------------------------------------------------------- Doji
def doji_level(cs: Candles, i: int) -> int:
    """2 — Doji на 100% (свеча целиком внутри соседней), 1 — маленькое тело, 0 — не Doji."""
    rng = cs.rng(i)
    if rng <= 0:
        return 0
    inside_prev = i >= 1 and cs.h[i] <= cs.h[i - 1] and cs.l[i] >= cs.l[i - 1]
    inside_next = i + 1 < cs.n and cs.h[i] <= cs.h[i + 1] and cs.l[i] >= cs.l[i + 1]
    if inside_prev or inside_next:
        return 2
    return 1 if abs(cs.c[i] - cs.o[i]) / rng <= P.DOJI_BODY_RATIO else 0


def find_dojis(cs: Candles, fus: list[FU] | None = None) -> list[int]:
    """Индексы Doji, которые НЕ забирают ликвидность (иначе это AFU, а не Doji)."""
    fus = fus if fus is not None else find_fus(cs)
    fu_i = {f.idx for f in fus}
    return [i for i in range(1, cs.n) if i not in fu_i and doji_level(cs, i) > 0]


# ---------------------------------------------------------------- OB
def find_obs(cs: Candles, tf: str | None = None, max_keep: int = 3) -> list[OB]:
    """Order Block: одиночная odd-свеча (почти без теней) против движения.

    Buy to Sell: бычья свеча внутри движения вниз -> OB для продаж (dir=SELL).
    Sell to Buy: медвежья свеча внутри движения вверх -> OB для покупок (dir=BUY).
    Зона = тело свечи. OB живёт, пока ни одна свеча не закрылась за его дальним краем.
    """
    tf = tf or cs.tf
    atr = cs.atr()
    res: list[OB] = []
    for i in range(1, cs.n - 1):
        rng = cs.rng(i)
        if rng <= 0:
            continue
        body_lo, body_hi = min(cs.o[i], cs.c[i]), max(cs.o[i], cs.c[i])
        wicks = (cs.h[i] - body_hi) + (body_lo - cs.l[i])
        if wicks / rng > P.ODD_WICK_MAX or (body_hi - body_lo) < 0.15 * atr:
            continue
        nxt = slice(i + 1, min(cs.n, i + 4))
        if cs.c[i] > cs.o[i] and cs.c[i - 1] < cs.o[i - 1]:        # бычья среди падения
            if cs.l[nxt].min() <= cs.l[i] - P.OB_DISPLACEMENT_ATR * atr:
                if not np.any(cs.c[i + 1:] > body_hi + 0.0):         # не пробит закрытием выше
                    res.append(OB(tf, "SELL", float(body_lo), float(body_hi), i, cs.t[i]))
        elif cs.c[i] < cs.o[i] and cs.c[i - 1] > cs.o[i - 1]:      # медвежья среди роста
            if cs.h[nxt].max() >= cs.h[i] + P.OB_DISPLACEMENT_ATR * atr:
                if not np.any(cs.c[i + 1:] < body_lo - 0.0):
                    res.append(OB(tf, "BUY", float(body_lo), float(body_hi), i, cs.t[i]))
    out: list[OB] = []
    for d in ("BUY", "SELL"):
        out.extend(sorted((o for o in res if o.dir == d), key=lambda o: o.idx)[-max_keep:])
    return out


# ---------------------------------------------------------------- свечные модели книги
def candle_models(cs: Candles, i: int = -1) -> list[str]:
    """Свечные модели книги на свече i (последней по умолчанию). Это карта стопов, не вход."""
    if i < 0:
        i += cs.n
    if i < 2 or i >= cs.n:
        return []
    o, h, l, c = cs.o, cs.h, cs.l, cs.c
    rng = cs.rng(i)
    if rng <= 0:
        return []
    body = abs(c[i] - o[i])
    up = h[i] - max(o[i], c[i])
    lo = min(o[i], c[i]) - l[i]
    trend_down = c[i - 1] < c[max(0, i - 5)]
    trend_up = c[i - 1] > c[max(0, i - 5)]
    out: list[str] = []
    small = body <= 0.35 * rng
    if small and lo >= 2 * max(body, 1e-9) and up <= 0.15 * rng:
        out.append("Hammer" if trend_down else "Hanging Man" if trend_up else "Hammer?")
    if small and up >= 2 * max(body, 1e-9) and lo <= 0.15 * rng and trend_up:
        out.append("Shooting Star")
    bull_i, bear_i = c[i] > o[i], c[i] < o[i]
    if bull_i and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1]:
        out.append("Bullish Engulfing")
    if bear_i and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1]:
        out.append("Bearish Engulfing")
    r2 = cs.rng(i - 2)
    b2 = abs(c[i - 2] - o[i - 2])
    b1 = abs(c[i - 1] - o[i - 1])
    if c[i - 2] < o[i - 2] and b2 >= 0.5 * r2 and b1 <= 0.3 * max(cs.rng(i - 1), 1e-9) and bull_i \
            and c[i] > (o[i - 2] + c[i - 2]) / 2:
        out.append("Morning Star")
    if c[i - 2] > o[i - 2] and b2 >= 0.5 * r2 and b1 <= 0.3 * max(cs.rng(i - 1), 1e-9) and bear_i \
            and c[i] < (o[i - 2] + c[i - 2]) / 2:
        out.append("Evening Star")
    if all(c[k] > o[k] for k in (i - 2, i - 1, i)) and c[i - 2] < c[i - 1] < c[i] \
            and o[i - 2] <= o[i - 1] <= c[i - 2] and o[i - 1] <= o[i] <= c[i - 1]:
        out.append("Three White Soldiers")
    if all(c[k] < o[k] for k in (i - 2, i - 1, i)) and c[i - 2] > c[i - 1] > c[i] \
            and c[i - 2] <= o[i - 1] <= o[i - 2] and c[i - 1] <= o[i] <= o[i - 1]:
        out.append("Three Black Crows")
    if doji_level(cs, i) > 0 and body / rng <= P.DOJI_BODY_RATIO:
        if up >= 0.25 * rng and lo >= 0.25 * rng and body / rng > 0.05:
            out.append("Spinning Top")
        else:
            out.append("Doji")
    return out
