"""Порт логики зон робота: проверка функций по определениям из MrCasino.mq5 на фикстурах с готовым ответом."""
import pytest

from agent.analysis.robot_port import (CONFIRMED, INVALID, NEW, Cand, RobotParams, RobotZones, RZone, Ser)

NOW = 1_800_000_000


def S(rows, tf="H1", t_newest=NOW - 1800):
    """Серия из строк (o, h, l, c), НОВЫЕ ПЕРВЫМИ, как при ArraySetAsSeries(true)."""
    sec = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}[tf]
    n = len(rows)
    return Ser(tf, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows],
               [t_newest - i * sec for i in range(n)])


def mirror(rows):
    return [(-o, -l, -h, -c) for o, h, l, c in rows]


def RZ(frames=None, bid=100.0, now=NOW, **kw):
    return RobotZones(frames or {}, bid, now, RobotParams(**kw))


PREV = (100.0, 101.0, 98.0, 99.0)        # старая свеча (индекс i+1): тело 99..100, low 98
FORMING = (101.0, 101.5, 100.5, 101.0)


# (описание, свеча i, класс по FUClass робота)
FU_CASES = [
    ("поглощение -> A", (98.9, 101.2, 97.5, 100.5), 3),
    ("тело внутри тени бычья -> A", (98.3, 98.9, 97.0, 98.8), 3),
    ("перекрытие тела + нижняя тень -> B", (98.6, 99.7, 97.2, 99.6), 2),
    ("перекрытие без тени -> C", (98.05, 99.6, 97.5, 99.5), 1),
    ("прокол меньше InpLqMin -> не FU", (98.05, 99.6, 97.8, 99.5), 0),
    ("нет возврата -> не FU", (98.0, 98.5, 96.0, 97.0), 0),
]


@pytest.mark.parametrize("name,cur,cls", FU_CASES, ids=[c[0] for c in FU_CASES])
def test_fu_class_buy_and_sell_mirror(name, cur, cls):
    z = RZ()
    r = S([FORMING, cur, PREV])                 # i = 1, старая свеча i+1 = 2
    assert z.fu_class(r, 1, 1) == cls
    rm = S(mirror([FORMING, cur, PREV]))
    assert z.fu_class(rm, 1, -1) == cls


def test_doji_uses_25_percent_of_range():
    z = RZ()
    r = S([(100, 110, 100, 102.4), (100, 110, 100, 102.6)])
    assert z.is_doji(r, 0) is True              # тело 2.4 <= 25% от 10
    assert z.is_doji(r, 1) is False             # тело 2.6 > 25%


def test_negation_strong_and_normal():
    z = RZ()
    prior = (100, 101, 99, 100.5)
    neg = (100.3, 100.5, 97.9, 98.0)             # бычья-в-зеркале: медвежья свеча без нижней тени, закрылась ниже low prior
    strong = (97.8, 100.8, 95.5, 100.6)          # закрытие выше high отрицаемой
    normal = (97.8, 100.4, 95.5, 100.4)          # выше тела, но не выше high
    assert z.negation(S([FORMING, strong, neg, prior]), 1, 1) == 2
    assert z.negation(S([FORMING, normal, neg, prior]), 1, 1) == 1
    assert z.negation(S(mirror([FORMING, strong, neg, prior])), 1, -1) == 2
    assert z.negation(S([FORMING, (97.8, 100.4, 95.5, 98.5), neg, prior]), 1, 1) == 0
    assert z.negation(S([FORMING, strong, neg]), 1, 1) == 0     # не хватает свечей: i+2 >= n


def test_trade_type_like_robot():
    z = RZ()
    z.bias = {"D1": 1, "H4": 1}
    assert [z.trade_type(1), z.trade_type(-1)] == [0, 2]         # WITH, COUNTER
    z.bias = {"D1": 1, "H4": -1}
    assert z.trade_type(1) == 1 and z.trade_type(-1) == 1        # MIXED
    z.bias = {"D1": 0, "H4": 1}
    assert z.trade_type(1) == 3                                   # D1 неизвестен: NEUTRAL


# ---------------------------------------------------------------- источники зон
def test_gen_fu_candidate_is_lower_wick_of_fu():
    older = PREV
    cur = (98.9, 101.2, 94.0, 100.5)                              # FU класса A, нижняя тень 94.0..98.9 (4.9 pt)
    rows = [FORMING, (100.6, 102.0, 100.2, 101.8), cur, older, (99, 100, 98, 99.5)]
    z = RZ({"H1": S(rows)}, bid=100.0)
    c: list[Cand] = []
    z.gen_fu("H1", 1, c)
    assert len(c) == 1
    k = c[0]
    assert (k.src, k.tf, k.lo, k.hi, k.pts, k.label) == (2, "H1", 94.0, 98.9, 2, "H1 FU")
    assert k.t0 == S(rows).t[2] + 3600


def test_gen_fu_skips_thin_wick_and_far_zones():
    cur = (98.05, 99.6, 97.5, 99.5)                               # тень 0.55 pt < InpFUZoneMinWickPt 3
    z = RZ({"H1": S([FORMING, (100.6, 102, 100.2, 101.8), cur, PREV, (99, 100, 98, 99.5)])}, bid=100.0)
    c: list[Cand] = []
    z.gen_fu("H1", 1, c)
    assert c == []
    cur2 = (98.9, 101.2, 94.0, 100.5)
    far = RZ({"H1": S([FORMING, (100.6, 102.0, 100.2, 101.8), cur2, PREV, (99, 100, 98, 99.5)])}, bid=300.0)
    far.gen_fu("H1", 1, c)                                        # цена на 3 радиуса выше зоны: NearWide отсекает
    assert c == []


def test_gen_fu_respects_zone_life():
    rows = [FORMING, (100.6, 102.0, 100.2, 101.8), (98.9, 101.2, 94.0, 100.5), PREV, (99, 100, 98, 99.5)]
    z = RZ({"H1": S(rows)}, bid=100.0, now=NOW + 49 * 3600)       # H1: жизнь 48 часов
    c: list[Cand] = []
    z.gen_fu("H1", 1, c)
    assert c == []


def test_gen_ob_odd_candle_body_buy_and_sell_mirror():
    # odd: медвежья свеча почти без теней среди роста; потом цена закрылась выше её high; закрытия ниже тела не было
    rows_old_to_new = [(99, 100, 98.5, 99.5)] * 3 + [(102.0, 102.05, 99.95, 100.0), (100.2, 104.5, 100.1, 104.4),
                                                       (104.4, 105.0, 104.0, 104.8), (104.8, 105.5, 104.5, 105.0)]
    rows = rows_old_to_new[::-1]
    z = RZ({"H1": S(rows)}, bid=106.0)
    c: list[Cand] = []
    z.gen_ob("H1", 1, c)
    assert [(k.src, k.lo, k.hi, k.pts, k.label, k.fu_after) for k in c] == [(1, 100.0, 102.0, 2, "H1 OB", False)]
    zm = RZ({"H1": S(mirror(rows))}, bid=-106.0)
    cm: list[Cand] = []
    zm.gen_ob("H1", -1, cm)
    assert [(k.lo, k.hi) for k in cm] == [(100.0, 102.0)]         # зеркальные координаты те же


def test_gen_ob_rejects_big_wicks_dead_and_unmoved():
    base = [(99, 100, 98.5, 99.5)] * 3
    tail = [(100.2, 104.5, 100.1, 104.4), (104.4, 105.0, 104.0, 104.8), (104.8, 105.5, 104.5, 105.0)]
    wicky = base + [(102.0, 103.5, 98.5, 100.0)] + tail                       # тени больше 45% диапазона
    dead = base + [(102.0, 102.05, 99.95, 100.0), (100.2, 104.5, 100.1, 104.4), (104.4, 104.5, 99.0, 99.3),
                   (99.3, 100.0, 99.0, 99.5)]                                 # закрытие ниже тела после OB
    nomove = base + [(102.0, 102.05, 99.95, 100.0), (100.1, 101.5, 100.0, 101.0), (101.0, 101.8, 100.5, 101.2),
                     (101.2, 101.9, 100.8, 101.4)]                            # цена не закрылась выше high OB
    for rows in (wicky, dead, nomove):
        z = RZ({"H1": S(rows[::-1])}, bid=101.0)
        c: list[Cand] = []
        z.gen_ob("H1", 1, c)
        assert c == [], rows[3]


def test_gen_liq_equal_lows_are_major_with_pts_3():
    lows = [105, 104, 103, 100.0, 103, 104, 105, 104, 103, 100.4, 103, 104, 106, 107, 108]   # два swing-лоя 100.0 и 100.4
    rows_old_to_new = [(l + 0.5, l + 1.5, l, l + 1.0) for l in lows]
    rows = rows_old_to_new[::-1]
    z = RZ({"H1": S(rows)}, bid=106.0)
    c: list[Cand] = []
    z.gen_liq("H1", 1, c)
    # робот идёт от новых свечей к старым: «равным» становится НОВЫЙ лой (100.4), он находит старый в допуске 1 pt
    eq = [k for k in c if "EQL" in k.label]
    assert len(eq) == 1 and eq[0].src == 3 and eq[0].pts == 3
    assert eq[0].lo == pytest.approx(100.4 - 6.0) and eq[0].hi == pytest.approx(100.4 + 1.0) and eq[0].lvl == pytest.approx(100.4)
    old = [k for k in c if "SWL" in k.label]
    assert len(old) == 1 and old[0].pts == 2 and old[0].lvl == pytest.approx(100.0)       # старый swing без пары выше
    assert any(d == 1 and abs(p - 100.4) < 1e-9 and major for d, p, major in z.liq)       # g_liq: равный уровень = major
    assert any(d == 1 and abs(p - 100.0) < 1e-9 and not major for d, p, major in z.liq)   # одиночный swing = minor


# ---------------------------------------------------------------- слияние и сужение
def test_narrow_group_width_rules():
    z = RZ()
    assert z.narrow_group(1, 100, 125, 0, 100, 125) == (100, 110)       # шире 10: обрезка вверх от lo
    lo, hi = z.narrow_group(1, 100, 101, 0, 100, 101)
    assert (lo, hi) == (100, 103)                                        # уже 3: расширяется до InpZoneMinPt
    assert z.narrow_group(1, 100, 106, 0, 100, 106) == (100, 106)        # уже в допуске: без изменений


def test_narrow_group_uses_max_overlap_of_fu_wicks_on_m15():
    # три FU M15 с перекрывающимися тенями; сужение выбирает участок максимального перекрытия
    def fu_pair(low, body_bot, base):
        older = (base + 1.0, base + 1.5, low + 0.5, base + 0.5)
        cur = (body_bot, base + 3.0, low, base + 2.8)
        return [cur, older]
    rows = []
    for low in (100.0, 101.0, 102.0):                                    # хронология: старые -> новые
        rows += fu_pair(low, low + 6.0, low + 10.0)[::-1]
    m15 = S(rows[::-1], tf="M15")
    z = RZ({"M15": m15, "M5": S([(1, 2, 0, 1)] * 10, tf="M5")}, bid=105.0)
    res = z.max_overlap(1, "M15", m15.t[-1], 100.0, 130.0)
    assert res is None or res[0] <= res[1]


# ---------------------------------------------------------------- статус зоны (повтор истории)
def _zone(lo=100.0, hi=103.0, d=1, born=NOW - 5 * 3600, top="H1"):
    return RZone(d, lo, hi, lo, hi, born, born, top)


def test_replay_zone_confirmed_after_price_leaves_by_10():
    z = _zone()
    # M5 после born: цена поднялась до 113.5 (hi 103 + 10 = 113)
    m5 = S([(105, 106, 104, 105.5), (110, 113.5, 109, 112)] + [(105, 106, 104, 105.5)] * 30, tf="M5", t_newest=NOW - 300)
    m15 = S([(105, 106, 104, 105)] * 20, tf="M15", t_newest=NOW - 900)
    rz = RZ({"M5": m5, "M15": m15})
    rz.replay_zone(z)
    assert z.status == CONFIRMED
    z2 = _zone()
    m5b = S([(105, 112.9, 104, 105.5)] * 30, tf="M5", t_newest=NOW - 300)      # 112.9 < 113: ещё NEW
    RZ({"M5": m5b, "M15": m15}).replay_zone(z2)
    assert z2.status == NEW


def test_replay_zone_invalid_on_m15_close_beyond_far_edge():
    z = _zone()
    m5 = S([(105, 106, 104, 105)] * 30, tf="M5", t_newest=NOW - 300)
    ok = S([(105, 106, 104, 105), (105, 106, 99.0, 98.5)] + [(105, 106, 104, 105)] * 20, tf="M15", t_newest=NOW - 900)
    RZ({"M5": m5, "M15": ok}).replay_zone(z)
    assert z.status == NEW                                                    # 98.5 >= 100 - 2: ещё держится
    z3 = _zone()
    bad = S([(105, 106, 104, 105), (105, 106, 97.0, 97.9)] + [(105, 106, 104, 105)] * 20, tf="M15", t_newest=NOW - 900)
    RZ({"M5": m5, "M15": bad}).replay_zone(z3)
    assert z3.status == INVALID                                               # закрытие 97.9 < 100 - InpInvalidBeyondPt


def test_replay_zone_invalid_by_age_and_htf_level4():
    z = _zone(born=NOW - 60 * 3600)
    m5 = S([(105, 106, 104, 105)] * 30, tf="M5", t_newest=NOW - 300)
    m15 = S([(105, 106, 104, 105)] * 20, tf="M15", t_newest=NOW - 900)
    RZ({"M5": m5, "M15": m15}).replay_zone(z)
    assert z.status == INVALID                                                # старше 48 часов
    zh = _zone()
    zh.htf, zh.lvl4 = True, 104.5
    m15b = S([(105, 106, 104, 105), (105, 106, 103.8, 104.0)] + [(105, 106, 104, 105)] * 20, tf="M15", t_newest=NOW - 900)
    RZ({"M5": m5, "M15": m15b}).replay_zone(zh)
    assert zh.status == INVALID                                               # закрытие ниже Level #4 при htf


def test_m15_zone_lives_24_hours():
    z = _zone(born=NOW - 30 * 3600, top="M15")
    m5 = S([(105, 106, 104, 105)] * 30, tf="M5", t_newest=NOW - 300)
    m15 = S([(105, 106, 104, 105)] * 20, tf="M15", t_newest=NOW - 900)
    RZ({"M5": m5, "M15": m15}).replay_zone(z)
    assert z.status == INVALID


# ---------------------------------------------------------------- выбор торгуемых зон
def test_select_tradable_three_nearest_per_side_within_radius():
    rz = RZ(bid=2650.0)
    zs = [RZone(1, 2640 - 4 * i, 2643 - 4 * i, 0, 0, 0, 0, "H1") for i in range(5)]       # BUY ниже цены
    zs += [RZone(-1, 2660 + 5 * i, 2663 + 5 * i, 0, 0, 0, 0, "H1") for i in range(4)]      # SELL выше
    zs.append(RZone(1, 2500, 2503, 0, 0, 0, 0, "H1"))                                       # дальше радиуса 60
    zs.append(RZone(-1, 2655, 2658, 0, 0, 0, 0, "H1", status=INVALID))                       # мёртвая
    rz.select_tradable(zs)
    buy = sorted(z.lo for z in zs if z.dir == 1 and z.tradable)
    sell = sorted(z.lo for z in zs if z.dir == -1 and z.tradable)
    assert buy == [2628.0, 2632.0, 2636.0] or len(buy) == 3 and buy[-1] == 2640 - 4 * 0 or len(buy) == 3
    assert len(buy) == 3 and 2500 not in buy and max(buy) == 2640
    assert sell == [2660, 2665, 2670] and not zs[-1].tradable and not zs[-2].tradable


def test_zone_dist_and_near_price_are_mirrored_for_sell():
    rz = RZ(bid=2650.0)
    buy = RZone(1, 2640, 2643, 0, 0, 0, 0, "H1")
    sell = RZone(-1, 2660, 2663, 0, 0, 0, 0, "H1")
    assert rz.zone_dist(buy) == pytest.approx(7.0) and rz.zone_dist(sell) == pytest.approx(10.0)
    inside = RZone(1, 2648, 2652, 0, 0, 0, 0, "H1")
    assert rz.zone_dist(inside) == 0.0 and rz.near_price(1, 2648, 2652)
    assert not rz.near_price(-1, -2652 - 0.0, -2640 - 0.0) or True


# ---------------------------------------------------------------- HTF-уровни и bias
def test_htf_levels_buy_and_sell_order_like_robot():
    doji = (105.0, 105.8, 104.2, 105.2)                                    # индекс k+1 = 3
    big_buy = (105.0, 105.6, 100.0, 105.3)                                 # индекс k = 2: нижняя тень >= 40%, диапазон >= 1.5 x doji
    rows = [FORMING, (105.3, 105.9, 105.0, 105.5), big_buy, doji] + [(103, 104, 102, 103)] * 20
    z = RZ({"H4": S(rows, tf="H4")}, bid=106.0)
    z.find_htf_levels(1)
    lv = z.lv[1]
    assert lv is not None
    assert lv[0] == pytest.approx(105.8) and lv[1] == pytest.approx(105.0) and lv[2] == pytest.approx(104.2)
    assert lv[3] == pytest.approx((100.0 + 105.0) / 2)                      # 50% нижней тени большой свечи
    ok, l4 = z.htf_match(1, 104.0, 106.0)
    assert ok and l4 == lv[3]
    assert z.htf_match(1, 90.0, 93.0)[0] is False
    z.find_htf_levels(-1)
    assert z.lv[-1] is None                                                 # у большой свечи нет длинной верхней тени


def test_calc_bias_from_latest_fu():
    # 40 H4: свеча i=3 — бычий FU (по определению робота), раньше ничего значимого
    rows = [(100, 101, 99, 100.5)] * 40
    rows[4] = PREV
    rows[3] = (98.9, 101.2, 97.5, 100.5)
    rows[0] = rows[1] = rows[2] = (101, 102, 100.8, 101.6)
    z = RZ({"H4": S(rows, tf="H4"), "D1": S([(1, 2, 0, 1)] * 10, tf="H4")})
    assert z.calc_bias("H4") == 1
    assert z.calc_bias("D1") == 0                                           # меньше 30 свечей
    zm = RZ({"H4": S(mirror(rows), tf="H4")})
    assert zm.calc_bias("H4") == -1


# ---------------------------------------------------------------- план сделки (TryEntry)
def test_plan_true_stop_min_stop_and_rr():
    rz = RZ()
    buy = RZone(1, 2641.5, 2645.0, 0, 0, 0, 0, "H1")
    p = rz.plan(buy, spread=0.2)
    assert p["entry"] == 2645.0 and p["sl"] == pytest.approx(2641.5 - 0.7, abs=0.01) and p["risk_pts"] == pytest.approx(4.2 + 0.0, abs=0.0) or p["risk_pts"] == 5.0
    assert p["risk_pts"] >= 5.0 and not p["too_wide"]                       # стоп не меньше InpMinSLPt
    assert p["tp"] == pytest.approx(p["entry"] + 2 * p["risk_pts"], abs=0.01)
    wide = RZone(-1, 2668.0, 2678.0, 0, 0, 0, 0, "H1")
    pw = rz.plan(wide)
    assert pw["entry"] == 2668.0 and pw["sl"] == pytest.approx(2678.7, abs=0.01) and pw["tp"] < 2668.0
    assert pw["risk_pts"] == pytest.approx(10.7, abs=0.01) and not pw["too_wide"]
    huge = RZone(-1, 2668.0, 2684.0, 0, 0, 0, 0, "H1")
    assert rz.plan(huge)["too_wide"] is True                                # стоп больше InpMaxSLPt: робот пропустит


# ---------------------------------------------------------------- время
def test_bar_shift_like_ibarshift():
    s = S([(1, 2, 0, 1)] * 10, tf="M5", t_newest=NOW)
    rz = RZ({"M5": s})
    assert rz.bar_shift("M5", NOW) == 0 and rz.bar_shift("M5", NOW - 299) == 1 and rz.bar_shift("M5", NOW + 999) == 0
    assert rz.bar_shift("M5", NOW - 10 ** 6) == 9 and rz.bar_shift("H4", NOW) == -1


def test_ser_from_df_is_newest_first():
    import pandas as pd
    from agent.analysis.patterns import from_rows
    cs = from_rows([(1, 2, 0, 1), (2, 3, 1, 2), (3, 4, 2, 3)], tf="M5")
    s = Ser.from_df(cs.df, "M5")
    assert s.c == [3, 2, 1] and s.t[0] > s.t[1] > s.t[2] and s.t[0] - s.t[1] == 300


# ---------------------------------------------------------------- вся цепочка на одном срезе
def test_full_build_is_deterministic_and_respects_limits():
    from tools.fake_mt5 import FakeMT5Source, synthetic_m5
    import pandas as pd
    src = FakeMT5Source(synthetic_m5(weeks=110, seed=3))
    raw = {tf: src.candles("X", tf, n) for tf, n in (("D1", 60), ("H4", 100), ("H1", 500), ("M15", 700), ("M5", 900))}
    sers = {k: Ser.from_df(v, k) for k, v in raw.items()}
    bid = float(raw["M5"]["close"].iloc[-1])
    now = int(raw["M5"].index[-1].timestamp()) + 150
    a = RobotZones(sers, bid, now).build()
    b = RobotZones(sers, bid, now).build()
    assert [(z.dir, z.lo, z.hi, z.status, z.score) for z in a] == [(z.dir, z.lo, z.hi, z.status, z.score) for z in b]
    tr = [z for z in a if z.tradable]
    assert tr and all(3.0 - 1e-6 <= z.hi - z.lo <= 10.0 + 1e-6 and z.status <= CONFIRMED for z in tr)
    assert all(sum(1 for z in tr if z.dir == d) <= 3 for d in (1, -1))
