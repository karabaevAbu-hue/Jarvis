"""События робота -> сообщения Абу (разделы 7 и 13 ТЗ): шаблоны, фильтр уровня, тихие часы, ветка сделки,
скрины, тревоги со звонком, аварийная пауза, накопление при обрыве интернета. Без ИИ.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.bridge.events import Event
from agent.brain.tools import OutMsg
from agent.config import Settings
from agent.storage.db import Database
from agent.templates import ALERT_TITLES, CRITICAL_KEYS, event_text, status_text
from agent.util import local_str, utcnow

log = logging.getLogger("notifier")

EMERGENCY_PAUSE_KEYS = {"no_sl", "orders_rejected"}      # единственное действие без «да»: пауза при аварии
TRADE_THREAD_TYPES = {"trade_be", "trade_modified", "trade_partial", "trade_closed"}
LOUD_TYPES = {"alert", "trade_opened", "trade_be", "trade_modified", "trade_partial", "trade_closed"}
BOT_REPORTS = {"morning_brief", "daily_report"}          # ассистент собирает их сам (reports.py)
OUTAGE_AFTER_SEC = 120


def in_quiet_hours(now: datetime, rng: tuple[dtime, dtime]) -> bool:
    a, b = rng
    t = now.timetz().replace(tzinfo=None)
    return (a <= t < b) if a <= b else (t >= a or t < b)


class Notifier:
    def __init__(self, settings: Settings, db: Database, messenger: Any, hub: Any, confirm: Any,
                 tts: Any = None, caller: Any = None, clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, emergency_delay: float = 5.0):
        self.s, self.db, self.m, self.hub, self.confirm = settings, db, messenger, hub, confirm
        self.tts, self.caller, self.clock, self.sleep = tts, caller, clock, sleep
        self.emergency_delay = emergency_delay
        self.open_alerts: dict[str, dict] = {}
        self.acked: set[str] = set()
        self._calls: dict[str, asyncio.Task] = {}
        self._emergency: dict[str, asyncio.Task] = {}
        self.backlog: list[tuple[str, str]] = []           # (время, текст), пока не было связи
        self.outage_since: datetime | None = None

    # ------------------------------------------------------------ опрос состояния для звонков
    def is_open(self, key: str) -> bool:
        return key in self.open_alerts

    def is_acked(self, key: str) -> bool:
        return key in self.acked

    def ack(self) -> None:
        """Абу написал «принял»: звонки по открытым тревогам прекращаются."""
        self.acked |= set(self.open_alerts)
        asyncio.get_running_loop().create_task(self.db.alert_ack_all())

    # ------------------------------------------------------------ вход
    async def on_event(self, ev: Event) -> None:
        if ev.type == "alert":
            await self._alert(ev)
            return
        if ev.level > self.s.msg_level:
            return                                          # фильтр «пиши меньше / пиши всё»
        if ev.type in BOT_REPORTS:
            return                                          # итоги и утренний план делает reports.py
        if ev.type == "cmd_result" and not ev.data.get("shot"):
            return                                          # ответ на команду уже передал ассистент
        st = self.hub.status()
        cur = st.get("account", "currency", default="USD") if st else "USD"
        text = event_text(ev, self.s.zone, cur)
        photo = self._shot(ev)
        reply_to = None
        ticket = ev.data.get("ticket")
        if ev.type in TRADE_THREAD_TYPES and ticket:
            reply_to = await self.db.get_trade_msg(int(ticket))
        silent = in_quiet_hours(self.clock().astimezone(self.s.zone), self.s.quiet_range()) and ev.type not in LOUD_TYPES
        outs = [OutMsg(text, photo=photo, voice=self.s.voice_trades and ev.type.startswith("trade_"))]
        mid = await self._deliver(outs, ev, reply_to, silent)
        if ev.type == "trade_opened" and ticket and mid:
            await self.db.set_trade_msg(int(ticket), mid)

    def _shot(self, ev: Event) -> bytes | None:
        shot = ev.data.get("shot")
        paths = self.hub.paths
        if not shot or paths is None:
            return None
        f = Path(paths.shots_dir) / str(shot)
        return f.read_bytes() if f.exists() else None

    async def _deliver(self, outs: list[OutMsg], ev: Event | None, reply_to: int | None, silent: bool) -> int | None:
        """Отправка с запасом на обрыв интернета: текст копится и уходит после восстановления."""
        try:
            mid = await self.m.send(outs, reply_to=reply_to, silent=silent, tts=self.tts)
            if self.backlog or self.outage_since:
                await self.flush_backlog()
            return mid
        except Exception as e:
            self.outage_since = self.outage_since or self.clock()
            when = local_str(self.clock(), self.s.zone)[11:16]
            self.backlog.extend((when, o.text) for o in outs if o.text)
            log.warning("не отправил (%s): %s, держу в очереди (%d)", type(e).__name__, ev.type if ev else "?",
                        len(self.backlog))
            return None

    async def flush_backlog(self) -> bool:
        """После восстановления связи: «связи не было с 14:10 до 14:25» + задержанные события."""
        if not self.backlog and self.outage_since is None:
            return True
        start = self.outage_since or self.clock()
        long_outage = (self.clock() - start).total_seconds() >= OUTAGE_AFTER_SEC
        lines = []
        if long_outage:
            a = local_str(start, self.s.zone)[11:16]
            b = local_str(self.clock(), self.s.zone)[11:16]
            lines.append(f"Связи не было с {a} до {b}.")
        if self.backlog:
            lines.append("Что пропустил:")
            lines += [f"{t} {x}" for t, x in self.backlog[:30]]
        if not lines:
            self.outage_since = None
            return True
        try:
            await self.m.text("\n".join(lines))
        except Exception:
            return False
        self.backlog.clear()
        self.outage_since = None
        return True

    # ------------------------------------------------------------ тревоги
    async def _alert(self, ev: Event) -> None:
        key = str(ev.data.get("key", "unknown"))
        state = ev.data.get("state", "open")
        sev = ev.data.get("severity") or ev.severity or "warning"
        st = self.hub.status()
        cur = st.get("account", "currency", default="USD") if st else "USD"
        text = event_text(ev, self.s.zone, cur)
        if state == "closed":
            self.open_alerts.pop(key, None)
            self.acked.discard(key)
            await self.db.alert_close(key)
            for d in (self._calls, self._emergency):
                t = d.pop(key, None)
                if t and not t.done():
                    t.cancel()
            await self._deliver([OutMsg(text)], ev, None, False)
            return
        if key in self.open_alerts:
            return                                          # повторное «open» той же тревоги не дублируем
        self.open_alerts[key] = {"severity": sev, "text": text}
        await self.db.alert_open(key, sev, text)
        await self._deliver([OutMsg(text)], ev, None, False)    # тревога всегда со звуком
        if sev == "critical":
            if key in EMERGENCY_PAUSE_KEYS:
                self._emergency[key] = asyncio.create_task(self._emergency_pause(key, text))
            if self.s.call_on_critical and self.caller is not None:
                self._calls[key] = asyncio.create_task(self._call(key, ev))

    async def raise_alert(self, key: str, state: str, severity: str, text: str) -> None:
        """Тревога от сторожа ассистента (MT5 закрыт, робот молчит): та же дорога, что у тревог робота."""
        await self.on_event(Event(seq=0, type="alert", level=0, severity=severity,
                                  data={"key": key, "state": state, "severity": severity}, text=text))

    def _spoken(self, key: str, ev: Event) -> str:
        title = ALERT_TITLES.get(key, key)
        st = self.hub.status()
        parts = [f"Тревога. {title}."]
        if ev.text and not ev.text.startswith("🚨"):
            parts.append(ev.text)
        if st and st.position:
            p = st.position
            parts.append(f"Открыта {p['side']} {p['lot']} лота, сейчас {p['profit']:+.0f} долларов.")
        elif st:
            parts.append("Позиции нет.")
        parts.append("Скажи, что делать.")
        return " ".join(parts)

    async def _call(self, key: str, ev: Event) -> None:
        try:
            res = await self.caller.call(key, self._spoken(key, ev), self.open_alerts.get(key, {}).get("text", ""))
            log.info("звонок по тревоге %s: %s", key, res)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("звонок по тревоге %s упал", key)

    async def _emergency_pause(self, key: str, alert_text: str) -> None:
        """Пауза без «да», только при открытой критичной тревоге и только если робот сам не встал на паузу."""
        await self.sleep(self.emergency_delay)
        if key not in self.open_alerts:
            return
        st = self.hub.status()
        if st is not None and st.paused:
            return
        res = await self.confirm.emergency_pause(f"тревога {key}")
        word = "Поставил паузу сам: " if res.ok else "Паузу поставить не вышло: "
        await self._deliver([OutMsg(f"⛔ {word}{res.msg}. Снять паузу можно только после твоего «да».")], None, None, False)
