"""Торговая сессия по времени UTC (ASIA / LONDON / NY)."""
from __future__ import annotations

from datetime import datetime

from agent.analysis import params as P


def session_name(dt_utc: datetime) -> str:
    h = dt_utc.hour
    for name, a, b in P.SESSIONS_UTC:
        if a <= h < b:
            return name
    return "OFF"
