"""Фигуры книги «Oddiy savdo kitobi» как карта ликвидности (Main Retail Focus).

По правилу Mr. Casino фигура — это место, где стоят стопы толпы, а не сигнал входа.
Поэтому каждая Figure хранит retail_view (что видит ретейл) и liq_price (где лежат стопы).
"""
from __future__ import annotations

import numpy as np

from agent.analysis.liquidity import equal_levels, swings
from agent.analysis.models import Figure
from agent.analysis.patterns import Candles

RETAIL_NOTE = "ретейл видит вход, для нас это стопы толпы"


def _fit(xs: list[int], ys: list[float]) -> tuple[float, float]:
    if len(xs) < 2:
        return 0.0, ys[0] if ys else 0.0
    s, b = np.polyfit(np.array(xs, float), np.array(ys, float), 1)
    return float(s), float(b)


def detect(cs: Candles, lookback: int = 120) -> list[Figure]:
    out: list[Figure] = []
    if cs.n < 30:
        return out
    atr = cs.atr()
    start = max(0, cs.n - lookback)
    hi, lo = swings(cs)
    hi = [i for i in hi if i >= start]
    lo = [i for i in lo if i >= start]

    # двойная / тройная вершина и дно
    for q in equal_levels(cs, lookback):
        if q.kind == "EQ_HIGH":
            name = "Тройная вершина" if q.touches >= 3 else "Двойная вершина"
            out.append(Figure(name, cs.tf, "SELL", "HIGH", q.price,
                              f"{name} {q.price:.1f}: стопы продавцов над уровнем, {RETAIL_NOTE}"))
        elif q.kind == "EQ_LOW":
            name = "Тройное дно" if q.touches >= 3 else "Двойное дно"
            out.append(Figure(name, cs.tf, "BUY", "LOW", q.price,
                              f"{name} {q.price:.1f}: стопы покупателей под уровнем, {RETAIL_NOTE}"))

    # голова и плечи (и обратная)
    if len(hi) >= 3:
        a, b, c = hi[-3:]
        neck = (cs.l[a:b + 1].min() + cs.l[b:c + 1].min()) / 2
        if cs.h[b] > cs.h[a] and cs.h[b] > cs.h[c] and abs(cs.h[a] - cs.h[c]) <= 0.3 * (cs.h[b] - neck) \
                and cs.h[b] - neck >= atr and not np.any(cs.h[c + 1:] > cs.h[c]):
            out.append(Figure("Голова и плечи", cs.tf, "SELL", "HIGH", float(cs.h[c]),
                              f"Голова и плечи: шея {neck:.1f}, правое плечо {cs.h[c]:.1f}; "
                              f"стопы над плечом, {RETAIL_NOTE}"))
    if len(lo) >= 3:
        a, b, c = lo[-3:]
        neck = (cs.h[a:b + 1].max() + cs.h[b:c + 1].max()) / 2
        if cs.l[b] < cs.l[a] and cs.l[b] < cs.l[c] and abs(cs.l[a] - cs.l[c]) <= 0.3 * (neck - cs.l[b]) \
                and neck - cs.l[b] >= atr and not np.any(cs.l[c + 1:] < cs.l[c]):
            out.append(Figure("Обратная голова и плечи", cs.tf, "BUY", "LOW", float(cs.l[c]),
                              f"Обратная голова и плечи: шея {neck:.1f}, правое плечо {cs.l[c]:.1f}; "
                              f"стопы под плечом, {RETAIL_NOTE}"))

    # клин / треугольник / прямоугольник по последним swing-точкам
    h4, l4 = hi[-4:], lo[-4:]
    if len(h4) >= 3 and len(l4) >= 3 and max(h4[-1], l4[-1]) - min(h4[0], l4[0]) >= 15:
        sh, _ = _fit(h4, [float(cs.h[i]) for i in h4])
        sl, _ = _fit(l4, [float(cs.l[i]) for i in l4])
        span = max(h4[-1], l4[-1]) - min(h4[0], l4[0])
        flat = 0.4 * atr / span
        last_hi, last_lo = float(cs.h[h4[-1]]), float(cs.l[l4[-1]])
        if abs(sh) < flat and abs(sl) < flat:
            out.append(Figure("Прямоугольник", cs.tf, "WAIT", "HIGH", last_hi,
                              f"Прямоугольник {last_lo:.1f}-{last_hi:.1f}: стопы над хаем и под лоем, {RETAIL_NOTE}"))
        elif sh < -flat and sl > flat:
            out.append(Figure("Симметричный треугольник", cs.tf, "WAIT", "LOW", last_lo,
                              f"Симметричный треугольник: стопы за обеими границами, {RETAIL_NOTE}"))
        elif sh > flat and sl > flat and sl > sh:
            out.append(Figure("Клин вверх", cs.tf, "SELL", "HIGH", last_hi,
                              f"Клин вверх: его хай {last_hi:.1f} снимут перед падением, {RETAIL_NOTE}"))
        elif sh < -flat and sl < -flat and sh < sl:
            out.append(Figure("Клин вниз", cs.tf, "BUY", "LOW", last_lo,
                              f"Клин вниз: его лой {last_lo:.1f} снимут перед ростом, {RETAIL_NOTE}"))

    # флаг: импульс >= 3 ATR, затем небольшой откат вдоль
    if cs.n >= 40:
        net = cs.c[-16] - cs.c[-36]
        seg_h, seg_l = cs.h[-15:], cs.l[-15:]
        if net >= 3 * atr:
            retr = (cs.c[-16] - seg_l.min()) / net
            if 0 < retr <= 0.5 and seg_h.max() <= cs.h[-16] + 0.3 * atr:
                out.append(Figure("Бычий флаг", cs.tf, "BUY", "LOW", float(seg_l.min()),
                                  f"Бычий флаг: стопы под лоем флага {seg_l.min():.1f}, {RETAIL_NOTE}"))
        elif net <= -3 * atr:
            retr = (seg_h.max() - cs.c[-16]) / -net
            if 0 < retr <= 0.5 and seg_l.min() >= cs.l[-16] - 0.3 * atr:
                out.append(Figure("Медвежий флаг", cs.tf, "SELL", "HIGH", float(seg_h.max()),
                                  f"Медвежий флаг: стопы над хаем флага {seg_h.max():.1f}, {RETAIL_NOTE}"))
    return out
