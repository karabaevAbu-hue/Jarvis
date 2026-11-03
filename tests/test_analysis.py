"""Ликвидность, bias, зоны, фигуры, график и движок целиком."""
import numpy as np
import pandas as pd
import pytest

from agent.analysis import params as P
from agent.analysis.chart import render_chart
from agent.analysis.engine import AnalysisEngine, compare_with_robot, render_text
from agent.analysis.figures import detect
from agent.analysis.liquidity import equal_levels, imbalances, nearest_major, trendlines
from agent.analysis.models import Liquidity
from agent.analysis.patterns import Candles, from_rows
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


# ---------------------------------------------------------------- фигуры = ликвидность
def test_double_top_figure_is_liquidity_not_entry():
    cs = from_rows(_wave({10: 105.0, 20: 104.97}, 40))
    figs = [f for f in detect(cs) if f.kind == "Двойная вершина"]
    assert figs and figs[0].liq_side == "HIGH" and figs[0].liq_price == pytest.approx(105.0)
    assert "стопы" in figs[0].text and "ретейл" in figs[0].text


# ---------------------------------------------------------------- движок целиком (зоны из порта робота)
@pytest.fixture(scope="module")
def result_and_engine():
    eng = AnalysisEngine(FakeMT5Source(synthetic_m5(weeks=110, seed=11)))
    return eng.analyze(), eng


def test_engine_zone_invariants(result_and_engine):
    res, eng = result_and_engine
    assert res.symbol == "XAUUSDm" and res.price == pytest.approx(2650.3, abs=0.5)
    assert res.zones, "на синтетическом рынке зоны должны находиться"
    assert sum(1 for z in res.zones if z.dir == "BUY") <= 2 and sum(1 for z in res.zones if z.dir == "SELL") <= 2
    for z in res.zones:
        assert 3.0 - 1e-6 <= z.width <= 10.0 + 1e-6                      # InpZoneMinPt..InpZoneMaxPt робота
        assert z.status in ("NEW", "CONFIRMED") and z.type in ("WITH", "MIXED", "COUNTER", "NEUTRAL")
        assert (z.lo < res.price) if z.dir == "BUY" else (z.hi > res.price)
        assert z.dist <= 60.0 + 1e-6                                     # радиус робота 60 pt
        pl = z.plan
        assert pl and (pl.sl < z.lo if z.dir == "BUY" else pl.sl > z.hi)
        assert pl.risk_pts >= 5.0 - 1e-6                                 # InpMinSLPt
        assert pl.tp > pl.entry_ref if z.dir == "BUY" else pl.tp < pl.entry_ref
        assert abs(pl.tp - pl.entry_ref) == pytest.approx(2.0 * pl.risk_pts, abs=0.02)   # RR 1:2
    # торгуемые зоны робота: не больше 3 на сторону
    for d in ("BUY", "SELL"):
        assert sum(1 for z in res.robot_zones if z["dir"] == d) <= 3


def test_side_filter(result_and_engine):
    _, eng = result_and_engine
    assert all(z.dir == "SELL" for z in eng.analyze(side="SELL").zones)


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
    mine = res.robot_zones
    assert mine
    z = mine[0]
    same = [{"dir": z["dir"], "lo": z["lo"] + 0.4, "hi": z["hi"] - 0.3, "status": z["status"]}]
    assert [d for d in compare_with_robot(res, same) if "робот" in d.lower() and "не построил" in d] == []
    far = [{"dir": z["dir"], "lo": z["lo"] + 3.0, "hi": z["hi"] + 2.0}]
    assert any("расхождение границ" in d or "не построил" in d for d in compare_with_robot(res, far))
    ghost = [{"dir": "BUY", "lo": 1000.0, "hi": 1004.0}]
    assert any("не построил" in d for d in compare_with_robot(res, ghost))
    # робот видит те же зоны, что и движок: расхождений нет
    assert compare_with_robot(res, [{"dir": m["dir"], "lo": m["lo"], "hi": m["hi"], "status": m["status"]} for m in mine]) == []


def test_engine_speed_under_30s():
    import time
    t = time.time()
    AnalysisEngine(FakeMT5Source(synthetic_m5(weeks=110, seed=5))).analyze()
    assert time.time() - t < 30
