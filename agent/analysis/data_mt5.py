"""Источники свечей и счёта: живой MT5 (пакет MetaTrader5, только чтение) и любой другой.

Источник описан протоколом CandleSource, поэтому движок одинаково работает на живом MT5 и на
tools/fake_mt5.py. Пакет MetaTrader5 есть только под Windows, импорт ленивый.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Protocol

import pandas as pd

log = logging.getLogger("data_mt5")

GOLD_CANDIDATES = ["XAUUSDm", "XAUUSDc", "XAUUSD", "GOLD#", "GOLD", "XAUUSD.a"]
# сколько свечей читает робот: M5 до 900 (повтор истории зоны), M15 до 700 (MaxOverlap), H1 130, H4 70, D1 40.
# W1 и D1 в зонах робота не участвуют (OB у него только H4, H1, M15); D1 нужен для bias.
TF_BARS = {"D1": 60, "H4": 100, "H1": 500, "M15": 700, "M5": 900}


class MT5Unavailable(RuntimeError):
    """MT5 не запущен или пакет недоступен — честное «MT5 не запущен»."""


class CandleSource(Protocol):
    def candles(self, symbol: str, tf: str, n: int) -> pd.DataFrame: ...
    def tick(self, symbol: str) -> dict[str, Any] | None: ...
    def account(self) -> dict[str, Any] | None: ...
    def positions(self) -> list[dict[str, Any]]: ...
    def find_symbol(self, preferred: str = "") -> str | None: ...


def rates_to_df(rates, offset_hours: float = 0.0) -> pd.DataFrame:
    """Структурированный массив MT5 -> DataFrame (UTC-индекс). Время MT5 — серверное,
    offset_hours вычитается, чтобы получить UTC."""
    df = pd.DataFrame(rates)
    idx = pd.to_datetime(df["time"], unit="s", utc=True) - pd.Timedelta(hours=offset_hours)
    out = df[["open", "high", "low", "close"]].copy()
    if "tick_volume" in df:
        out["volume"] = df["tick_volume"]
    out.index = idx
    out.index.name = "time"
    return out


class MT5Source:
    """Читает цены из уже запущенного терминала (без повторного входа в счёт)."""

    def __init__(self, path: str = "", server_offset_hours: float | None = None):
        self.path = path
        self._mt5 = None
        self._offset = server_offset_hours

    def _api(self):
        if self._mt5 is not None:
            return self._mt5
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as e:  # не Windows или пакет не установлен
            raise MT5Unavailable("пакет MetaTrader5 недоступен (нужен Windows + pip install MetaTrader5)") from e
        ok = mt5.initialize(path=self.path) if self.path else mt5.initialize()
        if not ok:
            raise MT5Unavailable(f"MT5 не запущен или не отвечает: {mt5.last_error()}")
        self._mt5 = mt5
        return mt5

    def shutdown(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    def _tf(self, name: str):
        mt5 = self._api()
        return {"M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
                "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1}[name]

    def server_offset(self, symbol: str) -> float:
        """Смещение серверного времени брокера от UTC в часах, по времени последнего тика."""
        if self._offset is not None:
            return self._offset
        t = self._api().symbol_info_tick(symbol)
        if not t:
            return 0.0
        self._offset = round((t.time - time.time()) / 3600.0)
        return self._offset

    def candles(self, symbol: str, tf: str, n: int) -> pd.DataFrame:
        mt5 = self._api()
        mt5.symbol_select(symbol, True)
        rates = mt5.copy_rates_from_pos(symbol, self._tf(tf), 0, n)
        if rates is None or len(rates) == 0:
            raise MT5Unavailable(f"нет свечей {symbol} {tf}: {mt5.last_error()}")
        return rates_to_df(rates, self.server_offset(symbol))

    def tick(self, symbol: str) -> dict[str, Any] | None:
        t = self._api().symbol_info_tick(symbol)
        if not t:
            return None
        return {"bid": t.bid, "ask": t.ask,
                "time": datetime.fromtimestamp(t.time - self.server_offset(symbol) * 3600, tz=timezone.utc)}

    def account(self) -> dict[str, Any] | None:
        a = self._api().account_info()
        return a._asdict() if a else None

    def positions(self) -> list[dict[str, Any]]:
        p = self._api().positions_get()
        return [x._asdict() for x in p] if p else []

    def history_deals(self, days: int = 7) -> list[dict[str, Any]]:
        mt5 = self._api()
        now = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(now - pd.Timedelta(days=days), now)
        return [d._asdict() for d in deals] if deals else []

    def data_path(self) -> str | None:
        """Папка данных терминала (там MQL5\\Logs)."""
        info = self._api().terminal_info()
        return info.data_path if info else None

    def find_symbol(self, preferred: str = "") -> str | None:
        mt5 = self._api()
        for name in ([preferred] if preferred else []) + GOLD_CANDIDATES:
            if mt5.symbol_info(name) is not None:
                mt5.symbol_select(name, True)
                return name
        return None


def load_frames(source: CandleSource, symbol: str, tfs: tuple[str, ...] = tuple(TF_BARS)) -> dict[str, pd.DataFrame]:
    """Свечи в объёме, который читает робот (см. TF_BARS)."""
    frames: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        try:
            frames[tf] = source.candles(symbol, tf, TF_BARS[tf])
        except MT5Unavailable:
            raise
        except Exception as e:
            log.warning("не загрузил %s %s: %s", symbol, tf, e)
    return frames
