"""Сборка зон: OB + FU + другие манипуляции (раздел 6 стратегии), слияние, баллы, план сделки.

Зона = тень FU / HCS / Negation, уточнённая внутри OB старшего ТФ; OB шире 10 пунктов служит
контекстом и добавляет баллы. Ширина зоны 3..10 пунктов (идеально 5).
"""
from __future__ import annotations

from agent.analysis import params as P
from agent.analysis.levels import zone_type
from agent.analysis.liquidity import nearest_major
from agent.analysis.models import (HCS, Imbalance, Liquidity, OB, Plan, RetestLevel, Source, Zone, opposite)
from agent.analysis.patterns import (Candles, find_fus, find_hcs, find_negations, find_obs, find_retests, fu_valid)

LOOKBACK = {"W1": 100, "D1": 120, "H4": 120, "H1": 200, "M15": 250}


# ------------------------------------------------------------------ источники
def collect_sources(frames: dict[str, Candles]) -> tuple[list[Source], list[OB]]:
    """Все источники зон: OB (W1..H1), FU / re-test / HCS / Negation (H4..M15)."""
    src: list[Source] = []
    obs_all: list[OB] = []
    for tf in P.OB_TFS:
        cs = frames.get(tf)
        if cs is None or cs.n < 20:
            continue
        for o in find_obs(cs, tf):
            obs_all.append(o)
            src.append(Source("OB", tf, o.dir, o.lo, o.hi, P.W_OB[tf], f"{tf} OB"))

    for tf in P.FU_TFS:
        cs = frames.get(tf)
        if cs is None or cs.n < 20:
            continue
        atr = cs.atr()
        start = max(1, cs.n - LOOKBACK[tf])
        fus = [f for f in find_fus(cs, start=start) if fu_valid(cs, f)]
        good = [f for f in fus if f.cls in ("A", "B") and f.height >= P.FU_MIN_HEIGHT_ATR * atr]
        htf_obs = [o for o in obs_all if o.tf != tf]
        for f in good:
            w = P.W_FU[tf] + (P.W_FU_CLASS_A if f.cls == "A" else 0)
            src.append(Source("FU", tf, f.dir, f.wick_lo, f.wick_hi, w, f"{tf} FU", f"{f.kind} класс {f.cls}"))
        for r in find_retests(cs, good):
            f = r.fu
            src.append(Source("RETEST", tf, f.dir, f.wick_lo, f.wick_hi, P.W_RETEST, f"{tf} FU re-test"))
        for h in find_hcs(cs, fus, htf_obs=htf_obs):
            f = h.first
            if f.height < P.FU_MIN_HEIGHT_ATR * atr or h.retest.idx < start:
                continue
            w = P.W_HCS[h.count] + (P.W_HCS_IN_HTF_OB if h.in_htf_ob else 0)
            lab = f"{tf} HCS" if h.count == 1 else f"{tf} HCS x{h.count}"
            src.append(Source("HCS", tf, h.dir, f.wick_lo, f.wick_hi, w, lab,
                              "в OB старшего ТФ" if h.in_htf_ob else ""))
        fu_by = {(f.idx, f.dir): f for f in fus}
        for n in find_negations(cs, fus):
            if n.idx < start:
                continue
            f = fu_by.get((n.idx, n.dir))
            if f is None or f.height < P.FU_MIN_HEIGHT_ATR * atr:
                continue
            w = P.W_NEG_STRONG if n.strong else P.W_NEG
            lab = f"{tf} Negation x3" if n.count == 3 else f"{tf} Negation"
            src.append(Source("NEG", tf, n.dir, f.wick_lo, f.wick_hi, w + (1 if n.count == 3 else 0), lab,
                              "сильный" if n.strong else ""))
    return src, obs_all


# ------------------------------------------------------------------ слияние
def _overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float, gap: float = 0.0) -> bool:
    return a_lo <= b_hi + gap and b_lo <= a_hi + gap


def _clip(lo: float, hi: float, d: str, width: float = P.ZONE_MAX_W) -> tuple[float, float]:
    """Слишком широкий диапазон обрезается у «экстремума»: BUY — нижняя часть, SELL — верхняя."""
    if hi - lo <= width:
        return lo, hi
    return (lo, lo + width) if d == "BUY" else (hi - width, hi)


def _widen(lo: float, hi: float, d: str) -> tuple[float, float]:
    if hi - lo >= P.ZONE_MIN_W:
        return lo, hi
    return (lo, lo + P.ZONE_MIN_W) if d == "BUY" else (hi - P.ZONE_MIN_W, hi)


def _clusters(items: list[Source], gap: float = 0.5) -> list[list[Source]]:
    items = sorted(items, key=lambda s: s.lo)
    out: list[list[Source]] = []
    cur: list[Source] = []
    cur_hi = None
    for s in items:
        if cur and s.lo <= cur_hi + gap:
            cur.append(s)
            cur_hi = max(cur_hi, s.hi)
        else:
            if cur:
                out.append(cur)
            cur, cur_hi = [s], s.hi
    if cur:
        out.append(cur)
    return out


def merge_sources(sources: list[Source], d: str) -> list[Zone]:
    mine = [s for s in sources if s.dir == d]
    anchors = [s for s in mine if s.hi - s.lo <= P.ZONE_MAX_W]
    wide = [s for s in mine if s.hi - s.lo > P.ZONE_MAX_W]
    zones: list[Zone] = []
    used_wide: set[int] = set()

    for cl in _clusters(anchors):
        # зерно — самый «тяжёлый» источник, остальные добавляются, пока ширина <= 10
        members = sorted(cl, key=lambda s: (-s.weight, s.hi - s.lo))
        lo, hi = members[0].lo, members[0].hi
        chosen = [members[0]]
        for s in members[1:]:
            nlo, nhi = min(lo, s.lo), max(hi, s.hi)
            if nhi - nlo <= P.ZONE_MAX_W and _overlap(lo, hi, s.lo, s.hi, 0.5):
                lo, hi = nlo, nhi
                chosen.append(s)
        lo, hi = _widen(*_clip(lo, hi, d), d)
        z = Zone(d, round(lo, 2), round(hi, 2), "NEW", 0, "MIXED")
        _attach(z, [s for s in mine if _overlap(lo, hi, s.lo, s.hi) and _covers(z, s)])
        for i, s in enumerate(wide):
            if _overlap(lo, hi, s.lo, s.hi):
                used_wide.add(i)
        zones.append(z)

    # широкие OB, внутри которых нет других источников: берём край у экстремума
    for i, s in enumerate(wide):
        if i in used_wide:
            continue
        lo, hi = _widen(*_clip(s.lo, s.hi, d), d)
        z = Zone(d, round(lo, 2), round(hi, 2), "NEW", 0, "MIXED")
        _attach(z, [s])
        zones.append(z)
    return zones


def _covers(z: Zone, s: Source) -> bool:
    """Источник относится к зоне: он целиком внутри неё или зона целиком внутри него
    или перекрывает не меньше половины зоны."""
    inter = min(z.hi, s.hi) - max(z.lo, s.lo)
    return inter >= 0.5 * min(z.width, s.hi - s.lo) or (s.lo <= z.lo and s.hi >= z.hi)


def _attach(z: Zone, srcs: list[Source]) -> None:
    """Баллы: каждый вид источника (OB, FU, re-test, HCS, Negation) считается один раз по лучшему весу,
    плюс бонус за слияние таймфреймов (до +2). Много однотипных FU балл не раздувают."""
    z.sources = srcs
    by_kind: dict[str, int] = {}
    labels: dict[tuple[str, str], Source] = {}
    for s in srcs:
        by_kind[s.kind] = max(by_kind.get(s.kind, 0), s.weight)
        k = (s.kind, s.tf)
        if k not in labels or s.weight > labels[k].weight:
            labels[k] = s
    uniq = sorted(labels.values(), key=lambda s: -s.weight)
    tfs = {s.tf for s in srcs}
    z.score = sum(by_kind.values()) + min(max(len(tfs) - 1, 0), 2)
    z.srcs = [s.label for s in uniq]
    z.htf = any(s.tf in ("H4", "D1", "W1") for s in uniq)
    z.hcs = max([int(s.label.split("x")[-1]) if "x" in s.label else 1 for s in uniq if s.kind == "HCS"] or [0])
    if any(s.kind in ("RETEST", "HCS") for s in uniq):
        z.status = "CONFIRMED"


# ------------------------------------------------------------------ итоговая сборка
def build_zones(frames: dict[str, Candles], price: float, bias: dict[str, str], liq: list[Liquidity],
                levels: list[RetestLevel], imbs: list[Imbalance], atr_h1: float,
                max_per_side: int = 2) -> list[Zone]:
    sources, _ = collect_sources(frames)
    out: list[Zone] = []
    for d in ("BUY", "SELL"):
        for z in merge_sources(sources, d):
            if not _valid_side(z, price, atr_h1):
                continue
            z.type = zone_type(d, bias.get("d1", "NEUTRAL"), bias.get("h4", "NEUTRAL"))
            _score_extras(z, liq, levels, imbs, atr_h1)
            if z.score < 1:
                continue
            z.dist = round(price - z.hi if d == "BUY" else z.lo - price, 2)
            out.append(z)
    final: list[Zone] = []
    for d in ("BUY", "SELL"):
        side = sorted((z for z in out if z.dir == d), key=lambda z: (-z.score, abs(z.dist)))
        final.extend(_dedupe(side)[:max_per_side])
    return final


def _dedupe(zs: list[Zone]) -> list[Zone]:
    keep: list[Zone] = []
    for z in zs:
        if any(_overlap(z.lo, z.hi, k.lo, k.hi) for k in keep):
            continue
        keep.append(z)
    return keep


def _valid_side(z: Zone, price: float, atr_h1: float) -> bool:
    maxd = P.MAX_ZONE_DIST_ATR * atr_h1
    if z.dir == "BUY":
        return z.lo < price and price - z.hi <= maxd
    return z.hi > price and z.lo - price <= maxd


def _score_extras(z: Zone, liq: list[Liquidity], levels: list[RetestLevel], imbs: list[Imbalance],
                  atr_h1: float) -> None:
    reach = 4 * atr_h1
    if z.dir == "BUY":
        under = [q for q in liq if q.side == "LOW" and q.major and z.lo - reach <= q.price < z.lo + 0.01]
        under.sort(key=lambda q: -q.price)
    else:
        under = [q for q in liq if q.side == "HIGH" and q.major and z.hi - 0.01 < q.price <= z.hi + reach]
        under.sort(key=lambda q: q.price)
    if under:
        z.liquidity_under = under[0]
        z.score += P.W_LIQ_UNDER
        word = "под" if z.dir == "BUY" else "над"
        z.notes.append(f"{word} зоной {under[0].note} {under[0].price:.1f} (стопы толпы)")
    for lv in levels:
        if lv.dir == opposite(z.dir) and z.lo - 0.5 <= lv.price <= z.hi + 0.5:
            z.score += P.W_LEVEL
            z.srcs.append(f"{lv.tf} Re-test Level #{lv.n}")
            break
    for im in imbs:
        if _overlap(z.lo, z.hi, im.lo, im.hi):
            z.score += P.P_FVG
            z.notes.append("зона внутри имбаланса, разворот из имбаланса не берём")
            break
    if z.type == "COUNTER":
        z.score += P.P_COUNTER
        z.notes.append("против потока D1 и H4")


# ------------------------------------------------------------------ план сделки
def make_plan(z: Zone, liq: list[Liquidity], atr_h1: float, rr: float = P.RR) -> Plan:
    """Вход после манипуляции; True Stop за дальним краем зоны; тейк RR 1:2 и ближайшая LAL."""
    buf = min(P.SL_BUFFER_MAX, max(P.SL_BUFFER_MIN, 0.15 * atr_h1))
    if z.dir == "BUY":
        entry, sl = z.hi, z.lo - buf
        risk = entry - sl
        if risk < P.SL_MIN:
            sl, risk = entry - P.SL_MIN, P.SL_MIN
        tp = entry + rr * risk
        lal = nearest_major(liq, "BUY", entry)
        cancel = z.lo
    else:
        entry, sl = z.lo, z.hi + buf
        risk = sl - entry
        if risk < P.SL_MIN:
            sl, risk = entry + P.SL_MIN, P.SL_MIN
        tp = entry - rr * risk
        lal = nearest_major(liq, "SELL", entry)
        cancel = z.hi
    wide = risk > P.SL_MAX
    plan = Plan(round(entry, 2), round(sl, 2), round(tp, 2), round(lal.price, 2) if lal else None,
                round(risk, 2), round(cancel, 2), wide)
    z.plan = plan
    return plan
