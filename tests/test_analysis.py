"""Ликвидность, bias, зоны, фигуры, график и движок целиком."""
import numpy as np
import pandas as pd
import pytest

from agent.analysis import params as P
from agent.analysis.chart import render_chart
from agent.analysis.engine import AnalysisEngine, compare_with_robot, render_text
from agent.analysis.figures import detect
from agent.analysis.levels import calc_bias, htf_retest_levels, zone_type
from agent.analysis.liquidity import equal_levels, imbalances, nearest_major, trendlines
from agent.analysis.models import Liquidity, Source, Zone
from agent.analysis.patterns import Candles, from_rows
from agent.analysis.zones import make_plan, merge_sources
from tools.fake_mt5 import FakeMT5Source, synthetic_m5


def _wave(peaks: dict[int, float], n: int, base=100.0):
    """Свечи с пиками хаёв в заданных индексах; фон монотонно растёт, своих swing-хаёв у него нет."""
    rows = []
    for k in range(n):
        c = base + 0.05 * k
        h = peaks.get(k, c + 0.4)
        rows.append((c, h, c - 0.4, c + 0.05))
    return rows


# ---------------------------------------------------------------- ликвидность
def test_equal_highs_are_major_and_swept_ones_disappear():
    rows = _wave({10: 105.0, 20: 104.97}, 40)
    cs = from_rows(rows)
    eq = [q for q in equal_levels(cs) if q.kind == "EQ_HIGH"]
    assert len(eq) == 1 and eq[0].major and abs(eq[0].price - 105.0) < 1e-9 and eq[0].touches == 2
    rows2 = rows + [(100, 106.0, 99.5, 100.2)]            # цена забрала уровень
    assert [q for q in equal_levels(from_rows(rows2)) if q.kind == "EQ_HIGH"] == []


def test_dirty_double_top_is_minor():
    cs = from_rows(_wave({10: 105.0, 20: 104.8}, 40))
    q = [q for q in equal_levels(cs) if q.side == "HIGH"]
    assert q and not q[0].major and q[0].kind == "DIRTY_HIGH"


def test_trendline_needs_three_touches():
    def build(touch_idx):
        rows = []
        for k in range(40):
            line = 100 + 0.5 * k
            if k in touch_idx:
                rows.append((line + 1, line + 1.1, line, line + 1))
            else:
                rows.append((line + 3, line + 3.5, line + 2.5, line + 3.2))
        return from_rows(rows)

    three = [q for q in trendlines(build({6, 16, 26})) if q.side == "LOW"]
    assert three and three[0].major and three[0].touches == 3
    assert [q for q in trendlines(build({6, 16})) if q.side == "LOW"] == []


def test_fvg_bullish_unfilled_and_filled():
    rows = [(100, 101, 99, 100.5), (100.5, 104, 100.4, 103.5), (103.5, 106, 103.0, 105.5)] + [(105.5, 106, 105, 105.6)] * 6
    cs = from_rows([r for r in rows])
    # зазор между high первой (101) и low третьей (103.0) = 2.0: >= 0.3 ATR
    im = imbalances(cs)
    assert im and im[0].dir == "BUY" and (im[0].lo, im[0].hi) == (101.0, 103.0)
    filled = from_rows(rows + [(105, 105.2, 100.5, 101.0)])
    assert imbalances(filled) == []


def test_nearest_major_target_by_direction():
    liq = [Liquidity("EQ_HIGH", "HIGH", 2660, True, "H1"), Liquidity("DOJI", "HIGH", 2655, True, "H1"),
           Liquidity("AFU", "HIGH", 2652, False, "M15"), Liquidity("EQ_LOW", "LOW", 2640, True, "H1")]
    assert nearest_major(liq, "BUY", 2650).price == 2655      # minor AFU цель не определяет
    assert nearest_major(liq, "SELL", 2650).price == 2640


# ---------------------------------------------------------------- bias и тип зоны
def test_zone_type_matrix():
    assert zone_type("BUY", "BUY", "BUY") == "WITH"
    assert zone_type("BUY", "BUY", "SELL") == "MIXED"
    assert zone_type("BUY", "SELL", "SELL") == "COUNTER"
    assert zone_type("SELL", "SELL", "SELL") == "WITH"
    assert zone_type("SELL", "NEUTRAL", "SELL") == "MIXED"


def test_bias_follows_latest_valid_fu():
    # бычий FU класса A: поток вверх
    rows = [(100, 101, 99, 100.5)] * 12 + [PREV_ROW := (100.0, 101.0, 98.0, 99.0), (98.9, 101.2, 97.5, 100.5)]
    rows += [(100.5, 101.5, 100.2, 101.2)] * 3
    assert calc_bias(from_rows(rows))[0] == "BUY"


def test_bias_structure_fallback():
    closes = [100, 101, 103, 102, 101.5, 103.5, 105, 104, 103.5, 105.5, 107, 106, 105.5, 107.5, 109, 108]
    rows = [(c - 0.2, c + 0.4, c - 0.5, c) for c in closes]
    assert calc_bias(from_rows(rows * 1))[0] in ("BUY", "NEUTRAL")


# ---------------------------------------------------------------- HTF Re-test Levels
def test_htf_retest_levels_bearish_scenario():
    base = [(100 + 0.1 * (k % 3), 101, 99, 100) for k in range(30)]
    doji = (105.0, 105.8, 104.4, 105.2)                       # маленькое тело
    big = (105.2, 111.0, 104.0, 105.5)                        # большая свеча с длинной верхней тенью
    cs = from_rows(base + [doji, big] + [(104, 104.5, 103, 103.5)] * 3, tf="H4")
    lv = [l for l in htf_retest_levels(cs, price=103.5) if l.dir == "SELL"]
    assert [l.n for l in lv] == [1, 2, 3, 4]
    assert lv[0].price == 104.4 and lv[2].price == 105.8      # L1 лой Doji, L3 хай Doji
    assert lv[1].price == pytest.approx((105.0 + 105.2) / 2)  # L2 середина тела Doji
    assert lv[3].price == pytest.approx((111.0 + 105.5) / 2)  # L4 50% верхней тени
    # цена пробила Level #4 закрытием -> сценарий отменён
    cs2 = from_rows(base + [doji, big, (105.5, 109.0, 105.0, 108.9)], tf="H4")
    assert [l for l in htf_retest_levels(cs2, price=108.9) if l.dir == "SELL"] == []


# ---------------------------------------------------------------- слияние зон и план
def _src(kind, tf, d, lo, hi, w, label=None):
    return Source(kind, tf, d, lo, hi, w, label or f"{tf} {kind}")


def test_wide_ob_is_context_narrow_fu_is_the_zone():
    srcs = [_src("OB", "H4", "BUY", 2630.0, 2655.0, 2), _src("FU", "H1", "BUY", 2641.5, 2645.0, 2),
            _src("RETEST", "H1", "BUY", 2641.5, 2645.0, 1)]
    zs = merge_sources(srcs, "BUY")
    assert len(zs) == 1
    z = zs[0]
    assert (z.lo, z.hi) == (2641.5, 2645.0)                    # зона уточнена до тени FU
    assert z.status == "CONFIRMED" and z.score == 6 and z.stars == "★★★" and "H4 OB" in z.srcs and z.htf


def test_zone_width_clipped_to_max_and_widened_to_min():
    wide = merge_sources([_src("OB", "D1", "SELL", 2700.0, 2730.0, 3)], "SELL")[0]
    assert wide.width == P.ZONE_MAX_W and wide.hi == 2730.0    # SELL: берём верх
    wide_buy = merge_sources([_src("OB", "D1", "BUY", 2600.0, 2630.0, 3)], "BUY")[0]
    assert wide_buy.width == P.ZONE_MAX_W and wide_buy.lo == 2600.0
    thin = merge_sources([_src("FU", "H1", "BUY", 2641.0, 2641.6, 2)], "BUY")[0]
    assert thin.width == P.ZONE_MIN_W


def test_plan_true_stop_and_rr():
    z = Zone("BUY", 2641.5, 2645.0, "CONFIRMED", 4, "MIXED")
    liq = [Liquidity("DOJI", "HIGH", 2656.0, True, "H1")]
    pl = make_plan(z, liq, atr_h1=4.0)
    assert pl.sl == pytest.approx(2641.5 - 1.0 - 0) or pl.risk_pts >= P.SL_MIN
    assert pl.sl < z.lo                                        # стоп за дальним краем
    assert pl.tp == pytest.approx(pl.entry_ref + 2 * pl.risk_pts)
    assert pl.tp_lal == 2656.0 and pl.cancel == 2641.5 and not pl.stop_too_wide
    sell = make_plan(Zone("SELL", 2668.0, 2672.5, "NEW", 2, "MIXED"), [], 4.0)
    assert sell.sl > 2672.5 and sell.tp < 2668.0


def test_plan_flags_stop_wider_than_max():
    z = Zone("SELL", 2668.0, 2683.0, "NEW", 2, "MIXED")        # шире допустимого: стоп выходит за 15
    pl = make_plan(z, [], atr_h1=4.0)
    assert pl.stop_too_wide and pl.risk_pts > P.SL_MAX


# ---------------------------------------------------------------- фигуры = ликвидность
def test_double_top_figure_is_liquidity_not_entry():
    cs = from_rows(_wave({10: 105.0, 20: 104.97}, 40))
    figs = [f for f in detect(cs) if f.kind == "Двойная вершина"]
    assert figs and figs[0].liq_side == "HIGH" and figs[0].liq_price == pytest.approx(105.0)
    assert "стопы" in figs[0].text and "ретейл" in figs[0].text


# ---------------------------------------------------------------- движок целиком
@pytest.fixture(scope="module")
def result_and_engine():
    eng = AnalysisEngine(FakeMT5Source(synthetic_m5(weeks=110, seed=11)))
    return eng.analyze(), eng


def test_engine_zone_invariants(result_and_engine):
    res, _ = result_and_engine
    assert res.symbol == "XAUUSDm" and res.price == pytest.approx(2650.3, abs=0.5)
    assert res.zones, "на синтетическом рынке зоны должны находиться"
    assert sum(1 for z in res.zones if z.dir == "BUY") <= 2 and sum(1 for z in res.zones if z.dir == "SELL") <= 2
    for z in res.zones:
        assert P.ZONE_MIN_W - 1e-6 <= z.width <= P.ZONE_MAX_W + 1e-6
        assert z.status in ("NEW", "CONFIRMED") and z.type in ("WITH", "MIXED", "COUNTER")
        assert (z.lo < res.price) if z.dir == "BUY" else (z.hi > res.price)
        pl = z.plan
        assert pl and (pl.sl < z.lo if z.dir == "BUY" else pl.sl > z.hi)
        assert pl.risk_pts >= P.SL_MIN - 1e-6
        assert pl.tp > pl.entry_ref if z.dir == "BUY" else pl.tp < pl.entry_ref


def test_side_filter(result_and_engine):
    _, eng = result_and_engine
    only_sell = eng.analyze(side="SELL")
    assert all(z.dir == "SELL" for z in only_sell.zones)


def test_text_format_has_required_parts(result_and_engine):
    res, _ = result_and_engine
    txt = render_text(res)
    assert txt.startswith("Золото ") and "Bias: D1" in txt and "сессия" in txt
    assert "Решение за тобой, это анализ, не гарантия." in txt
    assert "SL" in txt and "TP" in txt and "отмена" in txt


def test_engine_reports_mt5_down_honestly():
    src = FakeMT5Source(synthetic_m5(weeks=3))
    src.fail = True
    from agent.analysis.data_mt5 import MT5Unavailable
    with pytest.raises(MT5Unavailable):
        AnalysisEngine(src).analyze()


def test_chart_png(result_and_engine):
    res, eng = result_and_engine
    png = render_chart(eng.last_frames["H1"].df, res, tf="H1")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000


def test_compare_with_robot_reports_difference_with_reason(result_and_engine):
    res, _ = result_and_engine
    z = res.zones[0]
    same = [{"dir": z.dir, "lo": z.lo + 0.4, "hi": z.hi - 0.3}]
    assert [d for d in compare_with_robot(res, same) if z.dir in d and "робот" in d.lower() and "не нашёл" in d] == []
    far = [{"dir": z.dir, "lo": z.lo + 3.0, "hi": z.hi + 2.0}]
    diff = compare_with_robot(res, far)
    assert any("робот" in d.lower() for d in diff)
    ghost = [{"dir": "BUY", "lo": 1000.0, "hi": 1004.0}]
    assert any("не нашёл" in d for d in compare_with_robot(res, ghost))


def test_engine_speed_under_30s():
    import time
    t = time.time()
    AnalysisEngine(FakeMT5Source(synthetic_m5(weeks=110, seed=5))).analyze()
    assert time.time() - t < 30
