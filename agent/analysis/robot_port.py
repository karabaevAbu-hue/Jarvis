"""Порт логики зон робота MrCasino.mq5 (v3.2/v3.3) на Python.

Это построчный перенос функций робота, поэтому зоны движка должны совпадать с зонами робота
(приёмка: расхождение не больше 1 пункта). Имена методов повторяют имена в MQL5:
IsFU -> is_fu, FUClass -> fu_class, GenOB -> gen_ob, UpsertGroup -> upsert и так далее.

Серии хранятся как в MQL5 при ArraySetAsSeries(true): индекс 0 — формирующаяся свеча, 1 — последняя
закрытая. Для SELL цены зеркалятся (аксессоры ov/cv/hv/lv с d = -1), как в роботе.

Отличия от робота, которые нельзя перенести:
 - у робота зоны живут между пересчётами (память); порт строит зоны с нуля по свечам, статус берёт
   повтором истории от момента образования (ReplayZone), как робот при создании зоны;
 - hasHCS (+2 к баллу) выставляется в EvalZone при проверке триггера B, в порт не входит;
 - статус USED (после сделки) порт не знает.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

NEW, CONFIRMED, USED, INVALID = 0, 1, 2, 3
STATUS_NAME = {NEW: "NEW", CONFIRMED: "CONFIRMED", USED: "USED", INVALID: "INVALID"}
T_WITH, T_MIXED, T_COUNTER, T_NEUTRAL = 0, 1, 2, 3
TYPE_NAME = {T_WITH: "WITH", T_MIXED: "MIXED", T_COUNTER: "COUNTER", T_NEUTRAL: "NEUTRAL"}
PERIOD_SEC = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800}
TF_RANK = {"H4": 3, "H1": 2, "M15": 1}


@dataclass(frozen=True)
class RobotParams:
    """Значения входных параметров робота v3.2 по умолчанию (в пунктах цены золота)."""
    zone_radius: float = 60.0         # InpZoneRadiusPt
    max_zones_side: int = 3           # InpMaxZonesSide
    use_m15_ob: bool = True           # InpUseM15OB
    ob_wick_pct: float = 45.0         # InpOBWickPct
    ob_move_bars: int = 6             # InpOBMoveBars
    ob_need_fu: bool = False          # InpOBNeedFU
    fu_zone_min_wick: float = 3.0     # InpFUZoneMinWickPt
    sweep_zone: float = 6.0           # InpSweepZonePt
    eq_tol: float = 1.0               # InpEqTolPt
    zone_max: float = 10.0            # InpZoneMaxPt
    zone_min: float = 3.0             # InpZoneMinPt
    confirm_away: float = 10.0        # InpConfirmAwayPt
    invalid_beyond: float = 2.0       # InpInvalidBeyondPt
    zone_life_hours: int = 48         # InpZoneLifeHours
    zone_life_m15: int = 24           # InpZoneLifeM15
    doji_body_pct: float = 25.0       # InpDojiBodyPct
    wick_pct: float = 40.0            # InpWickPct
    lq_min: float = 0.3               # InpLqMinPt
    trade_buy: bool = True
    trade_sell: bool = True
    # стоп и тейк (TryEntry)
    min_sl: float = 5.0               # InpMinSLPt
    max_sl: float = 15.0              # InpMaxSLPt
    sl_buffer: float = 0.5            # InpSLBufferPt (к нему добавляется спред)
    true_stop: bool = True            # InpTrueStop
    rr: float = 2.0                   # InpRR


class Ser:
    """Серия свечей в порядке MQL5: индекс 0 — самая новая."""

    __slots__ = ("tf", "n", "o", "h", "l", "c", "t")

    def __init__(self, tf: str, o, h, l, c, t):
        self.tf, self.o, self.h, self.l, self.c, self.t = tf, o, h, l, c, t
        self.n = len(o)

    @classmethod
    def from_df(cls, df: pd.DataFrame, tf: str) -> "Ser":
        ts = [int(x.timestamp()) for x in df.index]
        return cls(tf, df["open"].tolist()[::-1], df["high"].tolist()[::-1], df["low"].tolist()[::-1],
                   df["close"].tolist()[::-1], ts[::-1])

    @classmethod
    def empty(cls, tf: str) -> "Ser":
        return cls(tf, [], [], [], [], [])

    def head(self, n: int) -> "Ser":
        """Load(tf, n): n самых новых свечей."""
        if n >= self.n:
            return self
        return Ser(self.tf, self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.t[:n])

    # зеркальные аксессоры
    def ov(self, i: int, d: int) -> float:
        return self.o[i] if d > 0 else -self.o[i]

    def cv(self, i: int, d: int) -> float:
        return self.c[i] if d > 0 else -self.c[i]

    def hv(self, i: int, d: int) -> float:
        return self.h[i] if d > 0 else -self.l[i]

    def lv(self, i: int, d: int) -> float:
        return self.l[i] if d > 0 else -self.h[i]

    def btop(self, i: int, d: int) -> float:
        return max(self.ov(i, d), self.cv(i, d))

    def bbot(self, i: int, d: int) -> float:
        return min(self.ov(i, d), self.cv(i, d))

    def rng(self, i: int, d: int) -> float:
        return self.hv(i, d) - self.lv(i, d)


@dataclass
class Cand:
    src: int                 # 1 OB, 2 тень FU, 3 ликвидность
    tf: str
    lo: float                # зеркальные координаты
    hi: float
    t0: int                  # конец свечи-источника, epoch
    pts: int
    fu_after: bool
    lvl: float               # зеркальный уровень ликвидности
    label: str


@dataclass
class RZone:
    dir: int                 # +1 BUY, -1 SELL
    lo: float                # реальные цены
    hi: float
    ulo: float
    uhi: float
    born: int
    newest: int
    top_tf: str
    status: int = NEW
    htf: bool = False
    lvl4: float = 0.0
    src_score: int = 0
    score: int = 0
    srcs: str = ""
    lvl: list[float] = field(default_factory=list)
    tradable: bool = False

    # зеркальные границы
    def zlo(self) -> float:
        return self.lo if self.dir > 0 else -self.hi

    def zhi(self) -> float:
        return self.hi if self.dir > 0 else -self.lo

    @property
    def dir_name(self) -> str:
        return "BUY" if self.dir > 0 else "SELL"


class RobotZones:
    """Расчёт зон по правилам робота на заданном срезе свечей."""

    def __init__(self, frames: dict[str, Ser], bid: float, now: int, params: RobotParams = RobotParams()):
        self.f = frames
        self.bid = bid
        self.now = now
        self.p = params
        self.bias = {"D1": 0, "H4": 0}
        self.lv: dict[int, list[float] | None] = {1: None, -1: None}
        self.liq: list[tuple[int, float, bool]] = []     # (dir, реальная цена, major) — g_liq
        self.zones: list[RZone] = []

    # ------------------------------------------------------------------ данные
    def load(self, tf: str, n: int) -> Ser:
        return self.f.get(tf, Ser.empty(tf)).head(n)

    def bar_shift(self, tf: str, t: int) -> int:
        """iBarShift(sym, tf, t, false): индекс свечи, в которую попадает время t (0 — самая новая)."""
        s = self.f.get(tf)
        if s is None or s.n == 0:
            return -1
        for i in range(s.n):
            if s.t[i] <= t:
                return i
        return s.n - 1

    def life_sec(self, tf: str) -> int:
        return (self.p.zone_life_m15 if tf == "M15" else self.p.zone_life_hours) * 3600

    # ------------------------------------------------------------------ паттерны [§5]
    def is_doji(self, r: Ser, i: int) -> bool:
        if i < 0 or i >= r.n:
            return False
        rg = r.h[i] - r.l[i]
        if rg <= 0:
            return False
        return abs(r.c[i] - r.o[i]) <= self.p.doji_body_pct / 100.0 * rg

    def is_fu(self, r: Ser, i: int, d: int) -> bool:
        if i < 0 or i + 1 >= r.n:
            return False
        return r.lv(i, d) < r.lv(i + 1, d) - self.p.lq_min and r.cv(i, d) > r.lv(i + 1, d)

    def is_engulf(self, r: Ser, i: int, d: int) -> bool:
        return r.cv(i, d) > r.btop(i + 1, d) and r.ov(i, d) <= r.bbot(i + 1, d)

    def is_body_cover(self, r: Ser, i: int, d: int) -> bool:
        b1 = r.btop(i + 1, d) - r.bbot(i + 1, d)
        if b1 <= 0:
            return False
        ov = min(r.btop(i, d), r.btop(i + 1, d)) - max(r.bbot(i, d), r.bbot(i + 1, d))
        return ov >= 0.5 * b1

    def is_lower_wick(self, r: Ser, i: int, d: int) -> bool:
        rg = r.rng(i, d)
        if rg <= 0:
            return False
        return (r.bbot(i, d) - r.lv(i, d)) >= self.p.wick_pct / 100.0 * rg

    def is_biw(self, r: Ser, i: int, d: int) -> bool:
        return r.bbot(i, d) >= r.lv(i + 1, d) and r.btop(i, d) <= r.bbot(i + 1, d)

    def fu_class(self, r: Ser, i: int, d: int) -> int:
        """3 = A (SFU), 2 = B, 1 = C, 0 = не FU."""
        if not self.is_fu(r, i, d):
            return 0
        bull = r.cv(i, d) > r.ov(i, d)
        if self.is_biw(r, i, d) and bull:
            return 3
        if self.is_engulf(r, i, d):
            return 3
        if self.is_body_cover(r, i, d) and self.is_lower_wick(r, i, d):
            return 2
        return 1

    def negation(self, r: Ser, i: int, d: int) -> int:
        """0 нет, 1 обычный Negation, 2 Strong Negation. Сигнальная свеча i, отрицаемая i+1."""
        if i < 0 or i + 2 >= r.n:
            return 0
        if not (r.cv(i + 1, d) < r.ov(i + 1, d)):
            return 0
        rg1 = r.rng(i + 1, d)
        if rg1 <= 0:
            return 0
        if r.bbot(i + 1, d) - r.lv(i + 1, d) > 0.15 * rg1:
            return 0
        if not (r.cv(i + 1, d) < r.lv(i + 2, d)):
            return 0
        if not (r.cv(i, d) > r.ov(i, d)):
            return 0
        if not (r.lv(i, d) < r.lv(i + 1, d)):
            return 0
        if r.cv(i, d) > r.hv(i + 1, d):
            return 2
        if r.cv(i, d) > r.btop(i + 1, d):
            return 1
        return 0

    def is_swing_low(self, r: Ser, i: int, d: int) -> bool:
        if i < 3 or i + 2 >= r.n:
            return False
        low = r.lv(i, d)
        return low < r.lv(i - 1, d) and low < r.lv(i - 2, d) and low < r.lv(i + 1, d) and low < r.lv(i + 2, d)

    # ------------------------------------------------------------------ bias и HTF-уровни
    def calc_bias(self, tf: str) -> int:
        r = self.load(tf, 40)
        if r.n < 30:
            return 0
        for i in range(1, 21):
            nb, ns = self.negation(r, i, 1), self.negation(r, i, -1)
            if nb > 0 and ns == 0:
                return 1
            if ns > 0 and nb == 0:
                return -1
            fb, fs = self.is_fu(r, i, 1), self.is_fu(r, i, -1)
            if fb and not fs:
                return 1
            if fs and not fb:
                return -1
            if fb and fs:
                return 1 if r.c[i] > r.o[i] else -1
        return 0

    def trade_type(self, d: int) -> int:
        d1, h4 = self.bias["D1"], self.bias["H4"]
        if d1 == 0:
            return T_NEUTRAL
        if d == d1 and d == h4:
            return T_WITH
        if d == d1 or d == h4:
            return T_MIXED
        return T_COUNTER

    def find_htf_levels(self, d: int) -> None:
        """FindHTFLevels: пара «Doji (старшая) + большая свеча с тенью» на H4, уровни 1-4 (реальные цены)."""
        self.lv[d] = None
        r = self.load("H4", 60)
        n = r.n
        if n < 20:
            return
        k = 1
        while k <= 40 and k + 1 < n:
            if self.is_doji(r, k + 1):
                rg_d = r.h[k + 1] - r.l[k + 1]
                rg_b = r.h[k] - r.l[k]
                if not (rg_b < 1.5 * rg_d) and not (r.bbot(k, d) - r.lv(k, d) < self.p.wick_pct / 100.0 * rg_b):
                    m1, m3 = r.hv(k + 1, d), r.lv(k + 1, d)
                    self.lv[d] = [d * m1, d * (m1 + m3) / 2.0, d * m3, d * (r.lv(k, d) + r.bbot(k, d)) / 2.0]
                    return
            k += 1

    def htf_match(self, d: int, lo: float, hi: float) -> tuple[bool, float]:
        lv = self.lv[d]
        if lv is None:
            return False, 0.0
        for v in lv:
            if lo - 2.0 <= v <= hi + 2.0:
                return True, lv[3]
        return False, lv[3]

    # ------------------------------------------------------------------ FVG
    def zone_in_fvg(self, lo: float, hi: float) -> bool:
        r = self.load("M5", 150)
        n = r.n
        for k in range(3, n - 1):
            bot, top = r.h[k + 1], r.l[k - 1]                      # бычий
            if top - bot >= 3.0:
                filled = any(r.l[q] <= bot for q in range(k - 2, 0, -1))
                if not filled and lo >= bot and hi <= top:
                    return True
            bot, top = r.h[k - 1], r.l[k + 1]                      # медвежий
            if top - bot >= 3.0:
                filled = any(r.h[q] >= top for q in range(k - 2, 0, -1))
                if not filled and lo >= bot and hi <= top:
                    return True
        return False

    # ------------------------------------------------------------------ радиус
    def near_wide(self, d: int, lo: float, hi: float) -> bool:
        pm = d * self.bid
        return lo <= pm and (pm - hi) <= 3.0 * self.p.zone_radius

    def near_price(self, d: int, lo: float, hi: float) -> bool:
        pm = d * self.bid
        return lo <= pm and (pm - hi) <= self.p.zone_radius

    def zone_dist(self, z: RZone) -> float:
        pm = z.dir * self.bid
        if pm > z.zhi():
            return pm - z.zhi()
        if pm < z.zlo():
            return z.zlo() - pm
        return 0.0

    # ------------------------------------------------------------------ источники зон [§6.1]
    def gen_ob(self, tf: str, d: int, c: list[Cand]) -> None:
        r = self.load(tf, 70 if tf == "H4" else (130 if tf == "H1" else 200))
        n = r.n
        pts = 3 if tf == "H4" else (2 if tf == "H1" else 1)
        for i in range(2, n - 1):
            t0 = r.t[i] + PERIOD_SEC[tf]
            if self.now - t0 > self.life_sec(tf):
                break
            rg = r.rng(i, d)
            if rg <= 0 or not (r.cv(i, d) < r.ov(i, d)):
                continue
            top, bot = r.btop(i, d), r.bbot(i, d)
            if rg - (top - bot) > self.p.ob_wick_pct / 100.0 * rg:
                continue
            moved = any(r.cv(k, d) > r.hv(i, d) for k in range(i - 1, max(1, i - self.p.ob_move_bars) - 1, -1))
            if not moved:
                continue
            if any(r.cv(k, d) < bot for k in range(i - 1, 0, -1)):
                continue
            fu = any(self.is_fu(r, j, d) and r.lv(j, d) <= top for j in range(i - 1, max(1, i - 5) - 1, -1))
            if self.p.ob_need_fu and not fu:
                continue
            if not self.near_wide(d, bot, top):
                continue
            c.append(Cand(1, tf, bot, top, t0, pts, fu, 0.0, f"{tf} OB"))

    def gen_fu(self, tf: str, d: int, c: list[Cand]) -> None:
        r = self.load(tf, 80 if tf == "H1" else 200)
        n = r.n
        pts = 2 if tf == "H1" else 1
        for i in range(1, n - 1):
            t0 = r.t[i] + PERIOD_SEC[tf]
            if self.now - t0 > self.life_sec(tf):
                break
            if self.fu_class(r, i, d) < 1:
                continue
            a, b = r.lv(i, d), r.bbot(i, d)
            if b - a < self.p.fu_zone_min_wick:
                continue
            if not self.near_wide(d, a, b):
                continue
            c.append(Cand(2, tf, a, b, t0, pts, False, 0.0, f"{tf} FU"))

    def add_liq(self, d: int, price: float, major: bool) -> None:
        for k, (kd, kp, km) in enumerate(self.liq):
            if kd == d and abs(kp - price) < 0.05:
                if major:
                    self.liq[k] = (kd, kp, True)
                return
        self.liq.append((d, price, major))

    def gen_liq(self, tf: str, d: int, c: list[Cand]) -> None:
        r = self.load(tf, 80 if tf == "H1" else 200)
        n = r.n
        for i in range(1, n - 1):
            t0 = r.t[i] + PERIOD_SEC[tf]
            if self.now - t0 > self.life_sec(tf):
                break
            sw = self.is_swing_low(r, i, d)
            dj = (not sw) and self.is_doji(r, i) and not self.is_fu(r, i, 1) and not self.is_fu(r, i, -1)
            if not sw and not dj:
                continue
            low = r.lv(i, d)
            if any(r.lv(k, d) < low for k in range(1, i)):
                continue
            pts = 2
            nm = ("SWL" if sw else "DOJI") if d > 0 else ("SWH" if sw else "DOJI")
            if sw:
                lim = self.now - self.p.zone_life_hours * 3600
                for j in range(i + 1, n - 2):
                    if r.t[j] < lim:
                        break
                    if self.is_swing_low(r, j, d) and abs(r.lv(j, d) - low) <= self.p.eq_tol:
                        pts = 3
                        nm = "EQL" if d > 0 else "EQH"
                        break
            self.add_liq(d, d * low, pts == 3 or dj)
            lo, hi = low - self.p.sweep_zone, low + 1.0
            if not self.near_wide(d, lo, hi):
                continue
            c.append(Cand(3, tf, lo, hi, t0, pts, False, low, f"{tf} {nm} {d * low:.2f}"))

    # ------------------------------------------------------------------ слияние и сужение [§6.2]
    @staticmethod
    def cand_strength(c: Cand) -> int:
        return 10 + c.pts if c.src in (1, 3) else c.pts

    def max_overlap(self, d: int, tf: str, since: int, rl: float, rh: float) -> tuple[float, float] | None:
        """Участок региона, где перекрывается больше всего теней FU."""
        sh = self.bar_shift(tf, since)
        m = self.load(tf, min(max(sh + 2, 5), 700))
        A: list[float] = []
        B: list[float] = []
        for j in range(1, m.n - 1):
            if m.t[j] < since:
                break
            if self.fu_class(m, j, d) < 1:
                continue
            a, b = max(m.lv(j, d), rl), min(m.bbot(j, d), rh)
            if b - a < 0.01:
                continue
            A.append(a)
            B.append(b)
        k = len(A)
        if k == 0:
            return None
        P = sorted(A + B)
        best, bl, bh = 0, 0.0, 0.0
        for i in range(2 * k - 1):
            x1, x2 = P[i], P[i + 1]
            if x2 - x1 < 1e-9:
                continue
            cov = sum(1 for q in range(k) if A[q] <= x1 + 1e-9 and B[q] >= x2 - 1e-9)
            if cov > best:
                best, bl, bh = cov, x1, x2
            elif cov == best and cov > 0 and abs(x1 - bh) < 1e-9:
                bh = x2
        if best == 0:
            return None
        return bl, bh

    def narrow_group(self, d: int, alo: float, ahi: float, a_t0: int, lo: float, hi: float) -> tuple[float, float]:
        p = self.p
        if hi - lo > p.zone_max:
            rl, rh = max(lo, alo), min(hi, ahi)
            if rh - rl < 0.01:
                rl, rh = lo, hi
            for tf in ("M15", "M5"):
                if not (rh - rl > p.zone_max):
                    break
                ov = self.max_overlap(d, tf, a_t0, rl, rh)
                if ov is not None:
                    rl, rh = ov
            lo, hi = rl, rh
        if hi - lo > p.zone_max:
            hi = lo + p.zone_max
        if hi - lo < p.zone_min:
            hi = lo + p.zone_min
        return lo, hi

    # ------------------------------------------------------------------ статус зоны (повтор истории)
    def advance_new(self, z: RZone, r: Ser, j: int) -> None:
        if z.status != NEW:
            return
        if r.hv(j, z.dir) >= z.zhi() + self.p.confirm_away:
            z.status = CONFIRMED

    def check_invalid_m15(self, z: RZone, m: Ser, j: int) -> None:
        if z.status > CONFIRMED:
            return
        c = m.cv(j, z.dir)
        if c < z.zlo() - self.p.invalid_beyond:
            z.status = INVALID
            return
        if z.htf and c < z.dir * z.lvl4:
            z.status = INVALID

    def replay_zone(self, z: RZone) -> None:
        z.status = NEW
        sh5 = self.bar_shift("M5", z.born)
        m5 = self.load("M5", min(max(sh5 + 2, 5), 900))
        for j in range(m5.n - 1, 0, -1):
            if m5.t[j] >= z.born and z.status == NEW:
                self.advance_new(z, m5, j)
        sh15 = self.bar_shift("M15", z.born)
        m15 = self.load("M15", min(max(sh15 + 2, 5), 400))
        for j in range(m15.n - 1, 0, -1):
            if m15.t[j] >= z.born:
                self.check_invalid_m15(z, m15, j)
        if z.status <= CONFIRMED and self.now - z.newest > self.life_sec(z.top_tf):
            z.status = INVALID

    # ------------------------------------------------------------------ группа -> зона (UpsertGroup без памяти)
    def upsert(self, d: int, c: list[Cand], mem: list[int], ai: int, glo: float, ghi: float) -> RZone | None:
        nlo, nhi = self.narrow_group(d, c[ai].lo, c[ai].hi, c[ai].t0, glo, ghi)
        ob_p = fu_p = n_fu = liq_p = 0
        fu_after = False
        formed = 0
        top_tf, top_rank = "M15", 0
        s_ob = s_fu = s_lq = ""
        lv: list[float] = []
        for k in mem:
            ck = c[k]
            if k != ai and (ck.hi < nlo or ck.lo > nhi):
                continue
            if ck.src == 1:
                ob_p = max(ob_p, ck.pts)
                if ck.fu_after:
                    fu_after = True
                if ck.label not in s_ob:
                    s_ob += (" + " if s_ob else "") + ck.label
            elif ck.src == 2:
                fu_p = max(fu_p, ck.pts)
                n_fu += 1
                if ck.label not in s_fu:
                    s_fu += (" + " if s_fu else "") + ck.label
            else:
                liq_p = max(liq_p, ck.pts)
                if ck.label not in s_lq:
                    s_lq += (" + " if s_lq else "") + ck.label
                if len(lv) < 6 and nlo - 2.0 <= ck.lvl <= nhi + 2.0:
                    lv.append(d * ck.lvl)
            formed = max(formed, ck.t0)
            rk = TF_RANK.get(ck.tf, 1)
            if rk > top_rank:
                top_rank, top_tf = rk, ck.tf
        srcs = s_ob
        if s_fu:
            srcs += (" + " if srcs else "") + s_fu
        if s_lq:
            srcs += (" + " if srcs else "") + s_lq
        src_score = ob_p + fu_p + min(3, max(0, n_fu - 1)) + liq_p + (1 if fu_after else 0)
        rulo, ruhi = (glo, ghi) if d > 0 else (-ghi, -glo)
        zlo = round(nlo if d > 0 else -nhi, 2)
        zhi = round(nhi if d > 0 else -nlo, 2)
        if not self.near_price(d, zlo if d > 0 else -zhi, zhi if d > 0 else -zlo):
            return None                       # зона дальше радиуса не создаётся
        z = RZone(d, zlo, zhi, rulo, ruhi, formed, formed, top_tf, NEW, False, 0.0, src_score, 0, srcs, lv)
        z.htf, z.lvl4 = self.htf_match(d, z.lo, z.hi)
        bonus = (1 if z.htf else 0) + (-2 if self.zone_in_fvg(z.lo, z.hi) else 0)
        z.score = src_score + bonus
        self.replay_zone(z)
        return z

    def select_tradable(self, zones: list[RZone]) -> None:
        for z in zones:
            z.tradable = False
        for side in (-1, 1):
            for _ in range(self.p.max_zones_side):
                best, bd = None, float("inf")
                for z in zones:
                    if z.dir != side or z.tradable:
                        continue
                    if z.status > CONFIRMED or not self.near_price(z.dir, z.zlo(), z.zhi()):
                        continue
                    dist = self.zone_dist(z)
                    if dist < bd:
                        bd, best = dist, z
                if best is not None:
                    best.tradable = True

    # ------------------------------------------------------------------ полный пересчёт (RebuildZones)
    def build(self) -> list[RZone]:
        p = self.p
        self.bias = {"D1": self.calc_bias("D1"), "H4": self.calc_bias("H4")}
        self.liq = []
        zones: list[RZone] = []
        for d in (1, -1):
            if (d > 0 and not p.trade_buy) or (d < 0 and not p.trade_sell):
                continue
            self.find_htf_levels(d)
            c: list[Cand] = []
            self.gen_ob("H4", d, c)
            self.gen_ob("H1", d, c)
            if p.use_m15_ob:
                self.gen_ob("M15", d, c)
            self.gen_fu("H1", d, c)
            self.gen_fu("M15", d, c)
            self.gen_liq("H1", d, c)
            self.gen_liq("M15", d, c)
            n = len(c)
            used = [False] * n
            while True:
                ai, bst = -1, -1
                for k in range(n):
                    if used[k]:
                        continue
                    st = self.cand_strength(c[k])
                    if st > bst or (st == bst and ai >= 0 and c[k].t0 > c[ai].t0):
                        bst, ai = st, k
                if ai < 0:
                    break
                glo, ghi = c[ai].lo, c[ai].hi
                limit = max(2.0 * p.zone_max, ghi - glo)          # группа не шире 2 x InpZoneMax
                mem = [ai]
                used[ai] = True
                added = True
                while added:
                    added = False
                    for k in range(n):
                        if used[k]:
                            continue
                        if c[k].lo > ghi + 1.0 or c[k].hi < glo - 1.0:
                            continue
                        nl2, nh2 = min(glo, c[k].lo), max(ghi, c[k].hi)
                        if nh2 - nl2 > limit:
                            continue
                        glo, ghi = nl2, nh2
                        used[k] = True
                        mem.append(k)
                        added = True
                z = self.upsert(d, c, mem, ai, glo, ghi)
                if z is not None:
                    zones.append(z)
        self.select_tradable(zones)
        self.zones = zones
        return zones

    # ------------------------------------------------------------------ план сделки (TryEntry, режим TP_RR)
    def plan(self, z: RZone, spread: float = 0.2) -> dict:
        """Стоп и тейк так, как их считает TryEntry: True Stop за дальним краем зоны, стоп не меньше
        InpMinSL, не больше InpMaxSL, тейк = стоп x RR. Вход берётся у ближнего края зоны."""
        p, d = self.p, z.dir
        buf = spread + p.sl_buffer
        entry = z.hi if d > 0 else z.lo
        sl = (z.lo if d > 0 else z.hi) - d * buf if p.true_stop else entry - d * (buf + p.min_sl)
        dist = d * (entry - sl)
        if dist < p.min_sl:
            dist = p.min_sl
            sl = entry - d * dist
        too_wide = dist > p.max_sl
        tp = entry + d * p.rr * dist
        return {"entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2), "risk_pts": round(dist, 2),
                "too_wide": too_wide, "cancel": round(z.lo if d > 0 else z.hi, 2)}
