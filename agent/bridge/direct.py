"""Аварийное закрытие позиций робота напрямую через пакет MetaTrader5.

Единственный случай прямой сделки (раздел 4 ТЗ): робот не ответил на команду close, а Абу уже сказал «да».
Закрываются только позиции с magic этого робота. Работает только на Windows с запущенным MT5;
в облаке не запускалось, на демо-счёте проверяется при приёмке.
"""
from __future__ import annotations

import asyncio

from agent.brain.confirm import ExecResult


def close_robot_positions_sync(mt5, magic: int, symbol: str | None = None) -> ExecResult:
    positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    mine = [p for p in (positions or []) if p.magic == magic]
    if not mine:
        return ExecResult(False, "позиций робота у брокера не найдено")
    done, errors = 0, []
    for p in mine:
        tick = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": p.ticket,
               "price": tick.bid if buy else tick.ask, "deviation": 30, "magic": magic,
               "comment": "MC EMERGENCY", "type_filling": mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            done += 1
        else:
            errors.append(str(getattr(res, "retcode", "нет ответа")))
    if errors:
        return ExecResult(False, f"закрыто {done} из {len(mine)}, ошибки брокера: {', '.join(errors)}")
    return ExecResult(True, f"закрыто позиций: {done}")


def make_direct_closer(source, magic: int):
    """Возвращает async-функцию для make_executor(direct_close=...). source — MT5Source."""
    async def run() -> ExecResult:
        def work() -> ExecResult:
            return close_robot_positions_sync(source._api(), magic)
        try:
            return await asyncio.to_thread(work)
        except Exception as e:
            return ExecResult(False, f"MT5 недоступен: {type(e).__name__}")
    return run
