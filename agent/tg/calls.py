"""Голосовые звонки: Абу -> ассистент (диалог с перебиванием) и ассистент -> Абу (критичная тревога).

Логика диалога и повторов написана против CallBackend и проверена на двойнике. PyTgCallsBackend
(py-tgcalls 3.x, звонки 1:1) в облаке не запускался: его проверяет шаг 2 плана на ПК Абу.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from agent.brain.tools import OutMsg
from agent.speech.tts import TTS, split_sentences
from agent.speech.vad import Endpointer

log = logging.getLogger("calls")

SILENCE_HANGUP_SEC = 60.0
FILLER_AFTER_SEC = 2.5
LONG_TEXT = 350                  # длиннее не зачитываем, а присылаем в чат
ALERT_ATTEMPTS = 5
ALERT_ANSWER_SEC = 30
ALERT_REPEAT_SEC = 120


class CallBackend(Protocol):
    def on_incoming(self, cb: Callable[[int], Awaitable[None]]) -> None: ...
    def on_audio(self, cb: Callable[[bytes], None]) -> None: ...      # PCM 48 кГц моно s16 от собеседника
    def on_ended(self, cb: Callable[[], None]) -> None: ...
    async def accept(self, user_id: int) -> None: ...
    async def decline(self, user_id: int) -> None: ...
    async def place(self, user_id: int, timeout: float) -> bool: ...  # True: ответили
    async def play_pcm(self, user_id: int, pcm48k: bytes) -> None: ...
    async def stop_playback(self, user_id: int) -> None: ...
    async def hangup(self, user_id: int) -> None: ...


# ------------------------------------------------------------------ диалог в звонке
class CallSession:
    def __init__(self, backend: CallBackend, assistant: Any, stt: Any, tts: TTS, endpointer: Endpointer,
                 send_chat: Callable[[list[OutMsg]], Awaitable[Any]], status_line: Callable[[], str],
                 owner_name: str = "Абу", max_minutes: float = 15, silence_sec: float = SILENCE_HANGUP_SEC,
                 filler_after: float = FILLER_AFTER_SEC, clock: Callable[[], float] = time.monotonic):
        self.b, self.assistant, self.stt, self.tts, self.ep = backend, assistant, stt, tts, endpointer
        self.send_chat, self.status_line, self.owner_name = send_chat, status_line, owner_name
        self.max_sec, self.silence_sec, self.filler_after, self.clock = max_minutes * 60, silence_sec, filler_after, clock
        self.latencies: list[float] = []
        self.user_id = 0
        self._speaking: asyncio.Task | None = None
        self._respond: asyncio.Task | None = None
        self._audio: asyncio.Queue[bytes] = asyncio.Queue()
        self._ended = asyncio.Event()
        self._last_activity = clock()
        self._t_speech_end: float | None = None

    # --- приём звука (вызывается backend'ом, возможно из другого потока)
    def feed_audio(self, pcm: bytes) -> None:
        self._audio.put_nowait(pcm)

    def mark_ended(self) -> None:
        self._ended.set()

    async def run(self, user_id: int, greeting: str | None = None) -> str:
        """Ведёт звонок до конца. Возвращает причину: 'ended', 'silence', 'limit'."""
        self.user_id = user_id
        started = self.clock()
        self._last_activity = started
        self._speaking = asyncio.create_task(self.say(greeting if greeting is not None else self._greeting()))
        reason = "ended"
        try:
            while not self._ended.is_set():
                try:
                    pcm = await asyncio.wait_for(self._audio.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    pcm = None
                now = self.clock()
                if pcm:
                    for kind, utt in self.ep.feed(pcm):
                        if kind == "start":
                            self._last_activity = now
                            await self._barge_in()
                        elif kind == "end" and utt is not None:
                            self._last_activity = now
                            self._t_speech_end = now
                            self._respond = asyncio.create_task(self._handle_utterance(utt.pcm16k))
                busy = (self._speaking and not self._speaking.done()) or (self._respond and not self._respond.done()) \
                    or self.ep.in_speech
                if busy:
                    self._last_activity = now                       # пока говорим или думаем, тишина не считается
                if now - started > self.max_sec:
                    await self._farewell("Время разговора вышло. Пиши, я на связи.")
                    reason = "limit"
                    break
                if now - self._last_activity > self.silence_sec:
                    await self._farewell("Если что, я на связи.")
                    reason = "silence"
                    break
        finally:
            for t in (self._speaking, self._respond):
                if t and not t.done():
                    t.cancel()
            await self.b.hangup(user_id)
        return reason

    def _greeting(self) -> str:
        return f"Слушаю, {self.owner_name}. {self.status_line()}"

    async def _farewell(self, text: str) -> None:
        if self._speaking and not self._speaking.done():
            self._speaking.cancel()
        await self.say(text)

    async def _barge_in(self) -> None:
        """Абу заговорил: замолкаем и слушаем."""
        if self._speaking and not self._speaking.done():
            self._speaking.cancel()
            await self.b.stop_playback(self.user_id)

    # --- речь ассистента
    async def say(self, text: str) -> None:
        """Озвучивает текст по предложениям; следующее предложение синтезируется, пока играет текущее."""
        sentences = split_sentences(text)
        jobs = [asyncio.create_task(self.tts.pcm48k(s)) for s in sentences]
        try:
            for j in jobs:
                pcm = await j
                if pcm:
                    if self._t_speech_end is not None:
                        self.latencies.append(self.clock() - self._t_speech_end)
                        self._t_speech_end = None
                    await self.b.play_pcm(self.user_id, pcm)
        except asyncio.CancelledError:
            for j in jobs:
                j.cancel()
            raise

    async def _handle_utterance(self, pcm16k: bytes) -> None:
        res = await self.stt.transcribe_pcm16k(pcm16k)
        text = (res.text or "").strip()
        if not text:
            return
        filler = asyncio.create_task(self._filler())
        try:
            outs = await self.assistant.handle(text, confidence=res.confidence, voice=True)
        finally:
            filler.cancel()
        spoken, to_chat = [], []
        for o in outs:
            if o.photo or len(o.text) > LONG_TEXT:
                to_chat.append(o)
            elif o.text:
                spoken.append(o.text)
        if to_chat:
            await self.send_chat(to_chat)
            spoken.append("Прислал текстом и графиком в чат.")
        if spoken:
            if self._speaking and not self._speaking.done():
                await asyncio.wait({self._speaking})          # не обрываем речь, договорим и продолжим
            self._speaking = asyncio.create_task(self.say(" ".join(spoken)))

    async def _filler(self) -> None:
        await asyncio.sleep(self.filler_after)
        if not (self._speaking and not self._speaking.done()):
            self._speaking = asyncio.create_task(self.say("Сейчас смотрю."))


# ------------------------------------------------------------------ звонок по тревоге
class AlertCaller:
    """Критичная тревога: звонок; не ответил за 30 с, то голосовое + текст; повтор через 2 минуты, до 5 раз,
    пока Абу не ответит, не напишет «принял» или причина не уйдёт."""

    def __init__(self, backend: CallBackend, owner_id: int, make_session: Callable[[], CallSession],
                 send_text: Callable[[str], Awaitable[Any]], send_voice: Callable[[str], Awaitable[Any]],
                 is_open: Callable[[str], bool], is_acked: Callable[[str], bool],
                 sleep: Callable = asyncio.sleep, attempts: int = ALERT_ATTEMPTS,
                 answer_sec: float = ALERT_ANSWER_SEC, repeat_sec: float = ALERT_REPEAT_SEC):
        self.b, self.owner = backend, owner_id
        self.make_session, self.send_text, self.send_voice = make_session, send_text, send_voice
        self.is_open, self.is_acked, self.sleep = is_open, is_acked, sleep
        self.attempts, self.answer_sec, self.repeat_sec = attempts, answer_sec, repeat_sec
        self.history: list[tuple[str, int, str]] = []        # (ключ, попытка, итог)

    async def call(self, key: str, spoken: str, text: str) -> str:
        """spoken — что сказать в звонке, text — то же текстом для чата. Возвращает итог."""
        for n in range(1, self.attempts + 1):
            if not self.is_open(key):
                self.history.append((key, n, "closed"))
                return "closed"
            if self.is_acked(key):
                self.history.append((key, n, "acked"))
                return "acked"
            answered = await self.b.place(self.owner, self.answer_sec)
            if answered:
                self.history.append((key, n, "answered"))
                await self.make_session().run(self.owner, greeting=spoken)
                return "answered"
            self.history.append((key, n, "no_answer"))
            if n == 1:
                await self.send_voice(spoken)
                await self.send_text(text)
            else:
                await self.send_text(f"Снова звоню, тревога не снята ({n}/{self.attempts}).")
            if n < self.attempts:
                await self.sleep(self.repeat_sec)
        return "gave_up"


# ------------------------------------------------------------------ py-tgcalls 3.x (адаптер)
class PyTgCallsBackend:
    """Адаптер над py-tgcalls 3.x: звонки 1:1 через Pyrogram-клиент. В облаке не запускался.

    Допущения, которые проверяет шаг 2 плана на ПК Абу:
     - входящий звонок приходит как ChatUpdate.Status.INCOMING_CALL, принимается вызовом play();
     - исходящий звонок: play(user_id) с внешним источником звука;
     - звук собеседника приходит кадрами StreamFrames (Direction.INCOMING), свой звук уходит send_frame().
    """

    FRAME_BYTES = 960 * 2 * 1       # 20 мс при 48 кГц, моно, s16

    def __init__(self, app: Any):
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction
        from pytgcalls.types.raw import AudioParameters, AudioStream, Stream
        from ntgcalls import MediaSource
        self._ctx = dict(ChatUpdate=ChatUpdate, Device=Device, Direction=Direction, AudioParameters=AudioParameters,
                         AudioStream=AudioStream, Stream=Stream, MediaSource=MediaSource, filters=filters)
        self.calls = PyTgCalls(app)
        self._incoming: Callable[[int], Awaitable[None]] | None = None
        self._audio: Callable[[bytes], None] | None = None
        self._ended: Callable[[], None] | None = None
        self._stop_play = asyncio.Event()

    def on_incoming(self, cb): self._incoming = cb
    def on_audio(self, cb): self._audio = cb
    def on_ended(self, cb): self._ended = cb

    def _stream(self):
        c = self._ctx
        ext = c["AudioStream"](c["MediaSource"].EXTERNAL, "", c["AudioParameters"](48000, 1))
        return c["Stream"](microphone=ext, speaker=ext)

    async def start(self) -> None:
        c = self._ctx
        f = c["filters"]

        @self.calls.on_update(f.chat_update(c["ChatUpdate"].Status.INCOMING_CALL))
        async def _incoming(_, update):
            if self._incoming:
                await self._incoming(update.chat_id)

        @self.calls.on_update(f.chat_update(c["ChatUpdate"].Status.LEFT_CALL))
        async def _left(_, update):
            if self._ended:
                self._ended()

        @self.calls.on_update(f.stream_frame(c["Direction"].INCOMING, c["Device"].SPEAKER))
        async def _frames(_, update):
            if self._audio:
                for fr in update.frames:
                    self._audio(fr.frame)

        await self.calls.start()

    async def accept(self, user_id: int) -> None:
        await self.calls.play(user_id, self._stream())

    async def decline(self, user_id: int) -> None:
        await self.calls.leave_call(user_id)

    async def place(self, user_id: int, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.calls.play(user_id, self._stream()), timeout=timeout)
            return True
        except Exception as e:
            log.info("звонок не принят: %s", type(e).__name__)
            try:
                await self.calls.leave_call(user_id)
            except Exception:
                pass
            return False

    async def play_pcm(self, user_id: int, pcm48k: bytes) -> None:
        self._stop_play.clear()
        c = self._ctx
        for i in range(0, len(pcm48k), self.FRAME_BYTES):
            if self._stop_play.is_set():
                return
            await self.calls.send_frame(user_id, c["Device"].MICROPHONE, pcm48k[i:i + self.FRAME_BYTES])
            await asyncio.sleep(0.019)          # подача в реальном времени: 20 мс кадр

    async def stop_playback(self, user_id: int) -> None:
        self._stop_play.set()

    async def hangup(self, user_id: int) -> None:
        try:
            await self.calls.leave_call(user_id)
        except Exception:
            pass
