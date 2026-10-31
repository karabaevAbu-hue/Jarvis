"""Сборка ассистента: мост, мозг, Telegram, звонки, сторож, расписание в одну работающую систему.

Все внешние части (Telegram, MT5, голос, Claude) подаются снаружи, поэтому Runtime одинаково
собирается из настоящих (agent.main) и из двойников (тесты, режим --console).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent import reports
from agent.analysis.engine import AnalysisEngine
from agent.bridge.hub import BridgeHub, LazyHub
from agent.bridge.protocol import BridgePaths, discover_robots
from agent.brain.assistant import Assistant
from agent.brain.confirm import ConfirmManager
from agent.brain.llm import LLM
from agent.brain.memory import Memory
from agent.brain.tools import OutMsg, ToolContext, make_executor
from agent.config import Settings
from agent.notifier import Notifier, in_quiet_hours
from agent.scheduler import WEEKDAYS, Scheduler
from agent.speech.vad import Endpointer, make_vad
from agent.storage.db import Database
from agent.tg.calls import AlertCaller, CallSession
from agent.tg.client import Messenger, OwnerGuard
from agent.util import local_day, utcnow
from agent.watchdog import Watchdog

log = logging.getLogger("runtime")

TV_RE = r"(tradingview|трейдингвью|трейдинг вью|oanda|оанда)"


def find_robot(settings: Settings) -> BridgePaths | None:
    """Папка робота: из BRIDGE_ROBOT, иначе единственная найденная, иначе со свежим status.json."""
    root = settings.bridge_path
    if settings.bridge_robot:
        p = BridgePaths(root, settings.bridge_robot)
        return p if p.base.is_dir() else None
    found = discover_robots(root)
    if not found:
        return None
    if len(found) == 1:
        return BridgePaths(root, found[0])
    freshest = max(found, key=lambda r: (BridgePaths(root, r).status.stat().st_mtime
                                         if BridgePaths(root, r).status.exists() else 0))
    return BridgePaths(root, freshest)


class Runtime:
    def __init__(self, settings: Settings, db: Database, messenger: Messenger, source: Any, tts: Any, stt: Any,
                 backend: Any = None, llm: LLM | None = None, system_static: str | None = None,
                 clock: Callable[[], datetime] = utcnow, vad_factory: Callable[[], Any] = make_vad,
                 direct_close_factory: Callable[[str], Any] | None = None):
        self.s, self.db, self.m, self.source, self.tts, self.stt = settings, db, messenger, source, tts, stt
        self.backend, self.clock, self.vad_factory = backend, clock, vad_factory
        self.direct_close_factory = direct_close_factory
        self.hub = LazyHub()
        self.guard = OwnerGuard(settings.owner_id, db, self._notify_owner, settings.zone, clock)
        self.engine = AnalysisEngine(source, settings.symbol)
        self.confirm = ConfirmManager(db, make_executor(self.hub, None), clock=lambda: self.clock().timestamp(),
                                      who=settings.owner_name)
        self.ctx = ToolContext(settings, db, self.hub, self.confirm, engine=self.engine, now=clock,
                               log_path=self._ea_log_path)
        self.llm = llm or LLM(settings, db, clock=clock)
        self.memory = Memory(db, settings, clock)
        self.assistant = Assistant(settings, self.ctx, self.llm, self.memory, system_static=system_static, clock=clock)
        self.notifier = Notifier(settings, db, messenger, self.hub, self.confirm, tts=tts, clock=clock)
        self.assistant.ack_callbacks.append(self.notifier.ack)
        self.watchdog = Watchdog(settings, self.hub, self.notifier, db, clock=clock)
        self.scheduler = Scheduler(settings, db, clock)
        self._session: CallSession | None = None
        self._tasks: list[asyncio.Task] = []
        self.sessions_log: list[dict] = []
        self._setup_calls()
        self._setup_schedule()

    # ------------------------------------------------------------ части
    async def _notify_owner(self, text: str) -> None:
        await self.m.text(text)

    def _ea_log_path(self) -> Path | None:
        try:
            data = self.source.data_path()
        except Exception:
            return None
        return Path(data) / "MQL5" / "Logs" / f"{self.clock().astimezone(self.s.zone):%Y%m%d}.log" if data else None

    def _status_line(self) -> str:
        st = self.hub.status()
        if st is None:
            return "Робот не отвечает."
        d = st.get("day", default={}) or {}
        cur = st.get("account", "currency", default="USD")
        state = "на паузе" if st.paused else "работает"
        return (f"Робот {state}, сделок {d.get('trades', 0)} из {d.get('max_trades', '?')}, "
                f"день {d.get('usd', 0):+.0f} {cur}.")

    def _make_session(self) -> CallSession:
        sess = CallSession(self.backend, self.assistant, self.stt, self.tts, Endpointer(self.vad_factory()),
                           lambda outs: self.m.send(outs), self._status_line, self.s.owner_name, self.s.call_max_min)
        self._session = sess
        return sess

    def _setup_calls(self) -> None:
        if self.backend is None:
            return
        self.backend.on_audio(lambda pcm: self._session and self._session.feed_audio(pcm))
        self.backend.on_ended(lambda: self._session and self._session.mark_ended())
        self.backend.on_incoming(self.on_incoming_call)

        async def send_voice(text: str) -> None:
            ogg = await self.tts.ogg_opus(text) if self.tts else None
            if ogg:
                await self.m.voice(ogg)

        self.notifier.caller = AlertCaller(self.backend, self.s.owner_id, self._make_session, self.m.text,
                                           send_voice, self.notifier.is_open, self.notifier.is_acked)

    def _setup_schedule(self) -> None:
        s, sch = self.s, self.scheduler

        async def send(outs: list[OutMsg]) -> None:
            quiet = in_quiet_hours(self.clock().astimezone(s.zone), s.quiet_range())
            await self.m.send(outs, silent=quiet)

        async def morning():
            await send(await reports.morning_plan(self.ctx))

        async def evening():
            await send(await reports.evening_zones(self.ctx))

        async def daily():
            await send(await reports.daily_report(self.ctx))

        async def weekly():
            await send(await reports.weekly_report(self.ctx, self.llm, self.assistant.system_static))

        async def cleanup():
            if self.hub.inner:
                self.hub.inner.cleanup()
            await self.memory.new_day_cleanup()

        sch.add("morning", s.morning_hm, WEEKDAYS, morning)
        sch.add("evening_zones", s.evening_zones_hm, {6, 0, 1, 2, 3}, evening)     # вс-чт: на завтра
        sch.add("daily", s.report_hm, WEEKDAYS, daily)
        sch.add_weekly("weekly", s.weekly, weekly)
        sch.add("cleanup", "03:00", {0, 1, 2, 3, 4, 5, 6}, cleanup)

    # ------------------------------------------------------------ робот
    async def attach_robot(self, paths: BridgePaths) -> BridgeHub:
        paths.ensure()
        hub = BridgeHub(paths, self.db, self.s.bridge_key)
        hub.subscribe(self.notifier.on_event)
        self.hub.attach(hub)
        await hub.poll_once()                    # курсор событий ставим сразу: ответ на первую команду не потеряется
        if self.direct_close_factory:
            magic = paths.robot.split("_")[1] if "_" in paths.robot else "0"
            self.confirm.executor = make_executor(self.hub, self.direct_close_factory(magic))
        hub.start()
        self._tasks.append(asyncio.create_task(hub.heartbeat_loop()))
        log.info("робот найден: %s", paths.base)
        return hub

    async def wait_robot(self, timeout: float = 10.0) -> bool:
        """Ждёт, пока найдётся папка робота (для консоли и тестов)."""
        end = asyncio.get_running_loop().time() + timeout
        while not self.hub and asyncio.get_running_loop().time() < end:
            await asyncio.sleep(0.05)
        return bool(self.hub)

    async def robot_finder(self, every: float = 5.0) -> None:
        while not self.hub:
            p = find_robot(self.s)
            if p is not None:
                await self.attach_robot(p)
                await self.m.text(f"🔗 Нашёл робота: {p.robot}")
                return
            await asyncio.sleep(every)

    async def start(self) -> None:
        self._tasks += [asyncio.create_task(self.robot_finder()), asyncio.create_task(self.watchdog.run()),
                        asyncio.create_task(self.scheduler.run())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.hub.inner:
            await self.hub.inner.stop()
        await self.m.q.stop()

    # ------------------------------------------------------------ сообщения владельца
    async def on_message(self, user_id: int | None, username: str | None = None, *, text: str | None = None,
                         voice_path: str | None = None, forwarded: bool = False) -> list[OutMsg]:
        """Входящее сообщение. Чужим не отвечаем. Голосовое: Whisper, ответ голосом + коротким текстом."""
        if not self.guard.allowed(user_id):
            await self.guard.stranger(user_id, username)
            return []
        conf = 1.0
        if voice_path:
            res = await self.stt.transcribe_file(voice_path)
            text, conf = res.text, res.confidence
            if not text.strip():
                outs = [OutMsg("Не расслышал голосовое, повтори.")]
                await self.m.send(outs)
                return outs
        if not text:
            return []
        outs = await self.assistant.handle(text, forwarded=forwarded, confidence=conf, voice=False)
        if voice_path:
            for o in reversed(outs):
                if o.text and not o.photo and len(o.text) <= 400:
                    o.voice = True                                    # на голосовое отвечаем голосовым + текстом
                    break
        await self.m.send(outs, tts=self.tts)
        await self._tv_hint(text)
        return outs

    async def _tv_hint(self, text: str) -> None:
        import re
        if re.search(TV_RE, text, re.I) and not await self.db.kv_get("hint.tradingview"):
            await self.db.kv_set("hint.tradingview", True)
            await self.m.text("К слову: свечи на TradingView (особенно OANDA, начиная с 4H) отличаются от свечей "
                              "твоего брокера в MT5. Зоны и сигналы проверяй на одном и том же фиде.")

    # ------------------------------------------------------------ звонки
    async def on_incoming_call(self, user_id: int) -> str:
        if not self.guard.allowed(user_id):
            await self.backend.decline(user_id)
            await self.guard.stranger(user_id, None, "звонок")
            return "declined"
        await self.backend.accept(user_id)
        sess = self._make_session()
        reason = await sess.run(user_id)
        self.sessions_log.append({"reason": reason, "latencies": list(sess.latencies)})
        if sess.latencies:
            await self.db.kv_set("call.latency.last", [round(x, 2) for x in sess.latencies])
        return reason
