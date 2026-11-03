"""Telegram-аккаунт ассистента (userbot, MTProto). Слушает только OWNER_ID.

Логика (охрана владельца, очередь отправки 1 сообщение/с, FloodWait, нарезка текста) не зависит от
библиотеки и покрыта тестами. Под ней тонкий адаптер PyrogramTransport; под Telethon меняется только он.
Адаптер на живом Telegram в облаке не запускался: проверка шагом 2 плана на ПК Абу.
"""
from __future__ import annotations

import asyncio
import io
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from agent.brain.tools import OutMsg
from agent.storage.db import Database
from agent.util import local_day, utcnow

log = logging.getLogger("tg")

MAX_TEXT = 4000          # лимит Telegram 4096, берём с запасом
CAPTION_MAX = 1024
MIN_INTERVAL = 1.0       # не чаще 1 сообщения в секунду


class Transport(Protocol):
    async def send_text(self, text: str, reply_to: int | None, silent: bool) -> int: ...
    async def send_photo(self, png: bytes, caption: str, reply_to: int | None, silent: bool) -> int: ...
    async def send_voice(self, ogg: bytes, reply_to: int | None, silent: bool) -> int: ...


# ------------------------------------------------------------------ охрана владельца
class OwnerGuard:
    """Отвечает только владельцу. Чужие сообщения и звонки: в лог и одно уведомление Абу в сутки."""

    def __init__(self, owner_id: int, db: Database, notify: Callable[[str], Awaitable[Any]],
                 tz: Any = "Asia/Bishkek", clock: Callable = utcnow):
        self.owner_id, self.db, self.notify, self.tz, self.clock = owner_id, db, notify, tz, clock

    def allowed(self, user_id: int | None) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id

    async def stranger(self, user_id: int | None, username: str | None, what: str = "сообщение") -> None:
        log.warning("чужой %s от id=%s @%s проигнорирован", what, user_id, username)
        key = f"stranger.notified.{local_day(self.clock(), self.tz)}"
        if await self.db.kv_get(key):
            return
        await self.db.kv_set(key, True)
        who = f"@{username}" if username else f"id {user_id}"
        await self.notify(f"Писал или звонил незнакомый {who}. Я не отвечал.")


# ------------------------------------------------------------------ очередь отправки
class FloodWait(Exception):
    """Универсальный FloodWait: адаптер превращает ошибку библиотеки в него (секунд ждать = value)."""

    def __init__(self, value: float):
        super().__init__(f"FloodWait {value}")
        self.value = value


@dataclass
class _Job:
    fn: Callable[[], Awaitable[Any]]
    fut: asyncio.Future


class SendQueue:
    """Последовательная отправка: интервал >= 1 с, при FloodWait ждём ровно столько, сколько сказано."""

    def __init__(self, min_interval: float = MIN_INTERVAL, sleep: Callable = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic, max_retries: int = 5):
        self.min_interval, self.sleep, self.clock, self.max_retries = min_interval, sleep, clock, max_retries
        self._q: asyncio.Queue[_Job] = asyncio.Queue()
        self._last = -1e9
        self._task: asyncio.Task | None = None
        self.waited_flood = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def submit(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        self.start()
        fut = asyncio.get_running_loop().create_future()
        await self._q.put(_Job(fn, fut))
        return await fut

    async def _worker(self) -> None:
        while True:
            job = await self._q.get()
            try:
                job.fut.set_result(await self._run(job.fn))
            except Exception as e:
                if not job.fut.done():
                    job.fut.set_exception(e)

    async def _run(self, fn):
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                await self.sleep(wait)
            try:
                res = await fn()
                self._last = self.clock()
                return res
            except FloodWait as e:
                self.waited_flood += e.value
                log.warning("FloodWait: жду %s с", e.value)
                await self.sleep(e.value + 1)
                self._last = self.clock()
        raise RuntimeError("FloodWait не прошёл после повторов")


def chunk_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Нарезка длинного текста по строкам, чтобы не упереться в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------------------------ сообщения владельцу
class Messenger:
    """Отправка владельцу: фото с подписью (до 1024 символов, иначе фото, потом текст), тихие часы, ветки."""

    def __init__(self, transport: Transport, queue: SendQueue | None = None):
        self.t = transport
        self.q = queue or SendQueue()

    async def text(self, text: str, reply_to: int | None = None, silent: bool = False) -> int | None:
        last = None
        for part in chunk_text(text):
            last = await self.q.submit(lambda p=part: self.t.send_text(p, reply_to, silent))
        return last

    async def photo(self, png: bytes, caption: str = "", reply_to: int | None = None, silent: bool = False) -> int:
        if len(caption) <= CAPTION_MAX:
            return await self.q.submit(lambda: self.t.send_photo(png, caption, reply_to, silent))
        mid = await self.q.submit(lambda: self.t.send_photo(png, "", reply_to, silent))
        await self.text(caption, reply_to, silent)
        return mid

    async def voice(self, ogg: bytes, reply_to: int | None = None, silent: bool = False) -> int:
        return await self.q.submit(lambda: self.t.send_voice(ogg, reply_to, silent))

    async def send(self, outs: list[OutMsg], reply_to: int | None = None, silent: bool = False,
                   tts: Any = None) -> int | None:
        """Список ответов ассистента владельцу. Возвращает id первого сообщения (для веток сделки)."""
        first: int | None = None
        for o in outs:
            mid = None
            if o.photo:
                mid = await self.photo(o.photo, o.text, reply_to, silent)
            elif o.text:
                mid = await self.text(o.text, reply_to, silent)
            if o.voice and tts is not None and o.text:
                ogg = await tts.ogg_opus(o.text)
                if ogg:
                    await self.voice(ogg, reply_to, silent)
            first = first if first is not None else mid
        return first


# ------------------------------------------------------------------ Pyrogram (адаптер)
class PyrogramTransport:
    """Тонкий адаптер над pyrofork/Pyrogram. Не запускался в облаке, см. шаг 2 плана."""

    def __init__(self, api_id: int, api_hash: str, phone: str, owner_id: int, workdir: str, name: str = "consigliere"):
        from pyrogram import Client
        self.owner_id = owner_id
        self.app = Client(name, api_id=api_id, api_hash=api_hash, phone_number=phone or None, workdir=workdir)

    async def start(self) -> None:
        await self.app.start()

    async def stop(self) -> None:
        await self.app.stop()

    async def owner_known(self) -> bool:
        """Знает ли аккаунт ассистента владельца (его access_hash в кэше сессии).

        У свежей сессии кэш пуст: писать по числовому id нельзя, пока Абу не написал ассистенту первым и
        обновление не попало в кэш. Прогреваем кэш диалогами; если Абу там нет, False (ассистент всё равно
        слушает и ответит, как только Абу напишет).
        """
        try:
            async for d in self.app.get_dialogs(limit=100):
                if d.chat and d.chat.id == self.owner_id:
                    return True
        except Exception as e:                                         # не мешаем запуску
            log.warning("диалоги не прочитаны: %s", e)
        try:
            await self.app.get_users(self.owner_id)
            return True
        except Exception:
            return False

    @staticmethod
    def _wrap(e: Exception) -> Exception:
        try:
            from pyrogram.errors import FloodWait as PgFlood
            if isinstance(e, PgFlood):
                return FloodWait(float(e.value))
        except ImportError:
            pass
        return e

    async def _do(self, coro):
        try:
            return await coro
        except Exception as e:
            raise self._wrap(e)

    async def send_text(self, text: str, reply_to: int | None, silent: bool) -> int:
        m = await self._do(self.app.send_message(self.owner_id, text, reply_to_message_id=reply_to,
                                                 disable_notification=silent))
        return m.id

    async def send_photo(self, png: bytes, caption: str, reply_to: int | None, silent: bool) -> int:
        bio = io.BytesIO(png)
        bio.name = "chart.png"
        m = await self._do(self.app.send_photo(self.owner_id, bio, caption=caption, reply_to_message_id=reply_to,
                                               disable_notification=silent))
        return m.id

    async def send_voice(self, ogg: bytes, reply_to: int | None, silent: bool) -> int:
        bio = io.BytesIO(ogg)
        bio.name = "voice.ogg"
        m = await self._do(self.app.send_voice(self.owner_id, bio, reply_to_message_id=reply_to,
                                               disable_notification=silent))
        return m.id

    # --- входящие
    def on_private(self, handler: Callable[[Any], Awaitable[None]]) -> None:
        from pyrogram import filters
        from pyrogram.handlers import MessageHandler
        self.app.add_handler(MessageHandler(handler, filters.private & ~filters.me))

    @staticmethod
    def is_forwarded(m: Any) -> bool:
        return bool(getattr(m, "forward_origin", None) or getattr(m, "forward_date", None)
                    or getattr(m, "forward_from", None) or getattr(m, "forward_from_chat", None))

    async def download_voice(self, m: Any, dest: str) -> str:
        return await m.download(file_name=dest)


# ------------------------------------------------------------------ пробник для --selftest
class PyrogramProbe:
    """Подключение к Telegram для самопроверки: не задаёт вопросов в консоли и ничего не запускает, кроме связи.

    connect() без authorize(): на неавторизованной сессии вернёт False, а не спросит код. Запись в сессию
    (peers) ведёт та же sqlite, поэтому при работающем ассистенте connect упадёт «database is locked».
    """

    def __init__(self, api_id: int, api_hash: str, phone: str, workdir: str, name: str = "consigliere"):
        from pyrogram import Client
        self.app = Client(name, api_id=api_id, api_hash=api_hash, phone_number=phone or None, workdir=workdir,
                          no_updates=True)

    async def connect(self) -> bool:
        try:
            return await self.app.connect()
        except (KeyError, ValueError, struct.error) as e:          # на рукопожатии пришёл не ответ Telegram
            raise ConnectionError("ответ сервера не похож на Telegram: трафик, вероятно, перехватывает прокси "
                                  "или фильтр, либо мешает брандмауэр") from e

    async def me(self) -> dict:
        m = await self.app.get_me()
        return {"id": m.id, "name": " ".join(x for x in (m.first_name, m.last_name) if x) or str(m.id),
                "username": m.username or "", "phone": getattr(m, "phone_number", "") or ""}

    async def owner(self, owner_id: int) -> dict:
        """Находит владельца: сначала в диалогах (кэш пиров), затем напрямую. PeerIdInvalid, если не знает."""
        async for d in self.app.get_dialogs(limit=100):
            if d.chat and d.chat.id == owner_id:
                c = d.chat
                return {"id": c.id, "name": " ".join(x for x in (c.first_name, c.last_name) if x) or str(c.id)}
        u = await self.app.get_users(owner_id)
        return {"id": u.id, "name": " ".join(x for x in (u.first_name, u.last_name) if x) or str(u.id)}

    async def send(self, owner_id: int, text: str) -> int:
        return (await self.app.send_message(owner_id, text)).id

    async def disconnect(self) -> None:
        try:
            await self.app.disconnect()
        except Exception:
            pass
