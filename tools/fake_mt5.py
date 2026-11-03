"""Свечи для разработки без Windows: синтетический XAUUSD или CSV.

FakeMT5Source реализует тот же протокол, что и MT5Source. Синтетический рынок — случайное
блуждание с режимами тренда и редкими «выносами стопов» (проколы экстремумов с возвратом),
чтобы в данных встречались FU, OB и ликвидность. Все ТФ получаются ресемплингом одних и тех же
M5-свечей, поэтому старшие и младшие таймфреймы согласованы.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

RULE = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D", "W1": "W-FRI"}


def synthetic_m5(weeks: int = 110, seed: int = 11, start_price: float = 2650.3,
                 end: str = "2026-10-30 21:00") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end_ts = pd.Timestamp(end, tz="UTC")
    idx = pd.date_range(end=end_ts, periods=weeks * 5 * 288 * 2, freq="5min", tz="UTC")
    idx = idx[idx.dayofweek < 5]                       # без выходных
    idx = idx[-weeks * 5 * 288:]
    n = len(idx)
    # режимы: дрейф меняется раз в ~2 дня
    regime = np.repeat(rng.normal(0, 0.012, n // 576 + 2), 576)[:n]
    vol = np.repeat(rng.uniform(0.35, 0.9, n // 288 + 2), 288)[:n]
    steps = regime + rng.normal(0, 1, n) * vol
    # выносы стопов: резкий прокол и возврат на следующей свече
    for s_i in np.flatnonzero(rng.random(n - 1) < 0.004):
        k = rng.choice([-1, 1]) * rng.uniform(2.5, 5.0)
        steps[s_i] += k
        steps[s_i + 1] -= 0.8 * k
    close = np.cumsum(steps)
    close = close - close[-1] + start_price           # последняя цена = start_price (ориентир ~2650)
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 1, (2, n))) * vol * 0.6
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def resample(m5: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "M5":
        return m5
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    out = m5.resample(RULE[tf], label="left" if tf != "W1" else "left", closed="left").agg(agg).dropna()
    return out


class FakeMT5Source:
    """Источник свечей из готового M5-DataFrame (или синтетики)."""

    def __init__(self, m5: pd.DataFrame | None = None, symbol: str = "XAUUSDm"):
        self.m5 = m5 if m5 is not None else synthetic_m5()
        self.symbol = symbol
        self._cache: dict[str, pd.DataFrame] = {}
        self.fail = False

    def _tf(self, tf: str) -> pd.DataFrame:
        if tf not in self._cache:
            self._cache[tf] = resample(self.m5, tf)
        return self._cache[tf]

    def candles(self, symbol: str, tf: str, n: int) -> pd.DataFrame:
        if self.fail:
            from agent.analysis.data_mt5 import MT5Unavailable
            raise MT5Unavailable("MT5 не запущен (симуляция)")
        return self._tf(tf).iloc[-n:].copy()

    def tick(self, symbol: str) -> dict[str, Any] | None:
        last = self.m5.iloc[-1]
        return {"bid": float(last["close"]), "ask": float(last["close"]) + 0.19, "time": self.m5.index[-1]}

    def account(self) -> dict[str, Any] | None:
        return {"login": 12345678, "balance": 1000.0, "equity": 1000.0, "currency": "USD"}

    def positions(self) -> list[dict[str, Any]]:
        return []

    def find_symbol(self, preferred: str = "") -> str | None:
        return preferred or self.symbol


class CsvSource(FakeMT5Source):
    """CSV с колонками time,open,high,low,close (M5), UTC."""

    def __init__(self, path: str | Path, symbol: str = "XAUUSDm"):
        df = pd.read_csv(path, parse_dates=["time"], index_col="time")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        super().__init__(df[["open", "high", "low", "close"]], symbol)
