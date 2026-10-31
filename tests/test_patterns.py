"""Паттерны на фикстурах с известным ответом: FU (7 типов, BUY и SELL), Negation, HCS, Doji, OB."""
import pytest

from agent.analysis import params as P
from agent.analysis.patterns import (Candles, candle_models, doji_level, find_dojis, find_fus, find_hcs,
                                     find_negations, find_obs, find_retests, fu_at, fu_valid, from_rows)

PREV = (100.0, 101.0, 98.0, 99.0)   # медвежья: тело 99..100, нижняя тень 98..99


def mirror(row):
    o, h, l, c = row
    return (-o, -l, -h, -c)


# (описание, свеча после PREV, тип, класс, SFU/AFU)
FU_CASES = [
    ("1 BIW+длинная тень", (98.3, 98.9, 97.0, 98.8), 1, "A", "SFU"),
    ("2 поглощение", (98.9, 101.2, 97.5, 100.5), 2, "A", "SFU"),
    ("3 перекрыто+тень", (98.6, 99.7, 97.2, 99.6), 3, "A", "AFU"),
    ("4 перекрыто", (98.05, 99.6, 97.9, 99.5), 4, "B", "AFU"),
    ("5 тень", (97.5, 98.7, 96.5, 98.6), 5, "B", "AFU"),
    ("6 развёрнуто", (97.4, 98.7, 97.3, 98.6), 6, "C", "AFU"),
    ("7 BIW", (98.8, 98.9, 97.95, 98.2), 7, "C", "AFU"),
]


@pytest.mark.parametrize("name,row,typ,cls,kind", FU_CASES, ids=[c[0] for c in FU_CASES])
def test_fu_types_bullish(name, row, typ, cls, kind):
    cs = from_rows([PREV, row])
    fus = fu_at(cs, 1)
    buy = [f for f in fus if f.dir == "BUY"]
    assert len(buy) == 1
    f = buy[0]
    assert (f.type, f.cls, f.kind) == (typ, cls, kind)
    assert f.level == 98.0                       # забранная ликвидность = low предыдущей
    assert f.wick_lo == row[2] and f.wick_hi == min(row[0], row[3])


@pytest.mark.parametrize("name,row,typ,cls,kind", FU_CASES, ids=[c[0] for c in FU_CASES])
def test_fu_types_bearish_mirror(name, row, typ, cls, kind):
    cs = from_rows([mirror(PREV), mirror(row)])
    sell = [f for f in fu_at(cs, 1) if f.dir == "SELL"]
    assert len(sell) == 1
    assert (sell[0].type, sell[0].cls, sell[0].kind) == (typ, cls, kind)


def test_no_fu_without_grab_or_return():
    # low выше low предыдущей: ликвидность не взята
    assert fu_at(from_rows([PREV, (98.5, 99.5, 98.2, 99.2)]), 1) == []
    # взята, но закрытие осталось ниже: не FU (вернулась не внутри свечи)
    assert [f for f in fu_at(from_rows([PREV, (98.5, 98.6, 96.0, 97.0)]), 1) if f.dir == "BUY"] == []


def test_fu_dies_when_far_edge_closed_through():
    cs = from_rows([PREV, (98.3, 98.9, 97.0, 98.8), (98.8, 98.9, 96.0, 96.5)])
    f = [x for x in fu_at(cs, 1) if x.dir == "BUY"][0]
    assert not fu_valid(cs, f)


# ---------------------------------------------------------------- Negation
def test_strong_negation_bearish_to_bullish():
    rows = [(100, 101, 99, 100.5),
            (100.3, 100.5, 97.9, 98.0),       # бездумно пробила low, закрылась ниже, почти без нижней тени
            (97.8, 100.8, 95.5, 100.6)]       # поглотила и дала большую нижнюю тень
    cs = from_rows(rows)
    negs = find_negations(cs)
    assert len(negs) == 1 and negs[0].dir == "BUY" and negs[0].strong and negs[0].count == 2


def test_weak_negation_does_not_engulf():
    rows = [(100, 101, 99, 100.5), (100.3, 100.5, 97.9, 98.0), (98.4, 99.0, 97.0, 98.5)]
    negs = find_negations(from_rows(rows))
    assert len(negs) == 1 and negs[0].dir == "BUY" and not negs[0].strong


def test_negation_bullish_to_bearish_mirror():
    rows = [mirror(r) for r in [(100, 101, 99, 100.5), (100.3, 100.5, 97.9, 98.0), (97.8, 100.8, 95.5, 100.6)]]
    negs = find_negations(from_rows(rows))
    assert len(negs) == 1 and negs[0].dir == "SELL" and negs[0].strong


def test_negation_of_negation_is_x3():
    rows = [(100, 101, 99, 100.5), (100.3, 100.5, 97.9, 98.0), (98.4, 99.0, 97.0, 98.5)]
    # третья свеча отрицает вторую: FU вверх от хая второй
    rows.append((98.4, 99.6, 98.3, 98.45))
    negs = find_negations(from_rows(rows))
    assert any(n.count == 3 and n.dir == "SELL" for n in negs)


# ---------------------------------------------------------------- HCS и re-test
FILL = [(101.0, 102.0, 100.5, 101.0)] * 3
HCS_ROWS = FILL + [
    (101, 102, 100.5, 100.8),      # p
    (100.6, 101.6, 98.0, 101.2),   # FU1 (BUY), тень 98.0..100.6
    (101.2, 103, 101.0, 102.8),    # ушли вверх
    (102.8, 104, 102.5, 103.5),
    (103.5, 103.6, 101.5, 102.0),  # откат
    (100.9, 102.2, 99.0, 101.9),   # FU2: low 99.0 внутри тени FU1 -> HCS
]


def test_hcs_is_fu_retested_with_fu():
    cs = from_rows(HCS_ROWS)
    fus = find_fus(cs)
    hcs = find_hcs(cs, fus)
    assert len(hcs) == 1
    h = hcs[0]
    assert (h.first.idx, h.retest.idx, h.count, h.dir) == (4, 8, 1, "BUY")
    assert not h.in_htf_ob


def test_hcs_x2_chain():
    rows = HCS_ROWS + [
        (101.9, 104, 101.8, 103.8), (103.8, 105, 103.5, 104.5), (104.5, 104.6, 102.5, 103.0),
        (103.0, 103.2, 100.3, 102.9),   # FU3: low 100.3 внутри теней FU1 и FU2
    ]
    cs = from_rows(rows)
    hcs = find_hcs(cs, find_fus(cs))
    assert max(h.count for h in hcs) == 2


def test_hcs_marks_inside_htf_ob():
    from agent.analysis.models import OB
    cs = from_rows(HCS_ROWS)
    ob = OB("H4", "BUY", 97.0, 101.0, 0, cs.t[0])
    hcs = find_hcs(cs, find_fus(cs), htf_obs=[ob])
    assert hcs[0].in_htf_ob
    ob_sell = OB("H4", "SELL", 97.0, 101.0, 0, cs.t[0])
    assert not find_hcs(cs, find_fus(cs), htf_obs=[ob_sell])[0].in_htf_ob


def test_hcs_not_counted_if_zone_broken_by_close():
    rows = list(HCS_ROWS)
    rows[7] = (103.5, 103.6, 97.0, 97.5)        # откат закрылся ниже тени FU1: зона мертва
    cs = from_rows(rows)
    assert find_hcs(cs, find_fus(cs)) == []


def test_retest_with_plain_candle_is_not_hcs():
    rows = list(HCS_ROWS)
    rows[-1] = (100.9, 102.3, 99.0, 102.2)      # возврат в зону, но low не ниже low предыдущей (не FU)
    rows[-2] = (103.5, 103.6, 98.9, 99.2)       # предыдущая свеча сама глубже
    cs = from_rows(rows)
    assert all(h.retest.idx != 8 for h in find_hcs(cs, find_fus(cs)))


def test_fu_retest_by_ordinary_rejection():
    rows = list(HCS_ROWS[:8]) + [
        (101.5, 102.2, 99.4, 101.9),            # зашла в тень FU1 (99.4) и отбилась нижней тенью, не FU
    ]
    cs = from_rows(rows)
    fus = [f for f in find_fus(cs) if f.idx == 4 and f.dir == "BUY"]
    assert find_retests(cs, fus) and find_retests(cs, fus)[0].idx == 8


# ---------------------------------------------------------------- Doji
def test_doji_inside_previous_is_100_percent():
    cs = from_rows([(100, 103, 98, 102), (100.5, 102.5, 99, 101.5)])
    assert doji_level(cs, 1) == 2


def test_big_body_is_not_doji():
    cs = from_rows([(100, 101, 99, 100.5)] * 2 + [(100, 105, 99.5, 104.5), (104.5, 108, 104, 107.5)])
    assert doji_level(cs, 2) == 0


def test_doji_that_grabs_liquidity_is_afu_not_doji():
    # свеча 1 — маленькое тело, но забрала low предыдущей и вернулась: это AFU, в Doji не входит
    rows = [(100, 101, 99, 100.5), (100.2, 100.6, 98.5, 100.3)]
    cs = from_rows(rows)
    assert doji_level(cs, 1) == 1
    assert 1 not in find_dojis(cs)


# ---------------------------------------------------------------- OB
def _down_filler(n=20):
    return [(112 - 0.5 * k + 0.2, 112 - 0.5 * k + 0.9, 112 - 0.5 * k - 0.6, 112 - 0.5 * k - 0.1) for k in range(n)]


def test_ob_buy_to_sell_is_body_of_odd_bullish_candle():
    rows = _down_filler() + [(102.0, 104.05, 101.95, 104.0),    # odd: бычья, почти без теней
                              (103.5, 103.6, 99.5, 99.8), (99.8, 100.0, 97.5, 97.8), (97.8, 98.0, 96.0, 96.3)]
    cs = from_rows(rows)
    obs = [o for o in find_obs(cs, "H1") if o.dir == "SELL"]
    assert len(obs) == 1
    assert (obs[0].lo, obs[0].hi, obs[0].idx) == (102.0, 104.0, 20)   # только тело


def test_ob_sell_to_buy_mirror():
    rows = [mirror(r) for r in _down_filler()] + [mirror(r) for r in [
        (102.0, 104.05, 101.95, 104.0), (103.5, 103.6, 99.5, 99.8), (99.8, 100.0, 97.5, 97.8), (97.8, 98.0, 96.0, 96.3)]]
    obs = [o for o in find_obs(from_rows(rows), "H1") if o.dir == "BUY"]
    assert len(obs) == 1 and (obs[0].lo, obs[0].hi) == (-104.0, -102.0)


def test_ob_dies_when_price_closes_beyond():
    rows = _down_filler() + [(102.0, 104.05, 101.95, 104.0), (103.5, 103.6, 99.5, 99.8),
                              (99.8, 106.0, 99.7, 105.5)]       # закрытие выше тела: OB пробит
    assert [o for o in find_obs(from_rows(rows), "H1") if o.dir == "SELL"] == []


def test_candle_with_big_wicks_is_not_odd():
    rows = _down_filler() + [(102.0, 106.0, 99.0, 104.0), (103.5, 103.6, 99.5, 99.8), (99.8, 100.0, 97.5, 97.8)]
    assert [o for o in find_obs(from_rows(rows), "H1") if o.dir == "SELL"] == []


# ---------------------------------------------------------------- свечные модели книги
def test_book_candle_models():
    down = [(110 - i, 110.4 - i, 109.4 - i, 109.6 - i) for i in range(6)]
    hammer = from_rows(down + [(103.0, 103.2, 100.0, 103.1)])
    assert "Hammer" in candle_models(hammer)
    engulf = from_rows(down + [(103.6, 103.7, 101.0, 102.8) , (102.0, 105.0, 101.8, 104.9)])
    assert "Bullish Engulfing" in candle_models(engulf)
    soldiers = from_rows(down + [(100.0, 101.6, 99.9, 101.5), (101.0, 102.6, 100.9, 102.5), (102.0, 103.6, 101.9, 103.5)])
    assert "Three White Soldiers" in candle_models(soldiers)
