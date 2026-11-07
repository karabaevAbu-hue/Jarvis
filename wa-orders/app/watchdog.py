"""Сторож номера и приём уведомлений GREEN-API (статусы отправленных, смена состояния)."""
from __future__ import annotations

import asyncio
import logging
import time

from .db import Database
from .engine import Engine, Notifier, Runtime
from .greenapi import GreenAPI, GreenAPIError

log = logging.getLogger(__name__)

STATE_RU = {
    "authorized": "✅ в сети",
    "notAuthorized": "⚠️ не подключён (нужен QR в консоли GREEN-API)",
    "blocked": "⛔ забанен WhatsApp",
    "suspended": "⛔ временно ограничен WhatsApp",
    "yellowCard": "⛔ временно ограничен WhatsApp",
    "sleepMode": "⚠️ телефон выключен или без сети",
    "starting": "⏳ инстанс запускается",
    "unknown": "❔ неизвестно (нет связи с GREEN-API)",
}

STATE_ALERT = {
    "authorized": "✅ Номер WhatsApp снова в сети.",
    "notAuthorized": "⚠️ Номер отвязан от GREEN-API. Переподключите его по QR в консоли. Очередь очищена, рассылка ждёт.",
    "blocked": "⛔ Номер ЗАБАНЕН WhatsApp. Рассылка остановлена, очередь очищена.",
    "suspended": (
        "⛔ WhatsApp временно ограничил номер (suspended). Рассылка остановлена, очередь очищена. "
        "После снятия ограничений запускайте с меньшей нагрузкой."
    ),
    "sleepMode": "⚠️ Телефон с номером выключен или без сети. Рассылка ждёт, пока он не вернётся.",
}
STATE_ALERT["yellowCard"] = STATE_ALERT["suspended"]

DESIRED_SETTINGS = {
    "webhookUrl": "",  # режим HTTP API: уведомления забираем сами через receiveNotification
    "outgoingWebhook": "yes",  # статусы отправленных
    "outgoingAPIMessageWebhook": "yes",
    "outgoingMessageWebhook": "no",
    "incomingWebhook": "no",  # входящие из 50 групп нам не нужны
    "stateWebhook": "yes",
    "delaySendMessagesMilliseconds": 10000,  # страховка; ниже нашей минимальной паузы
}


async def ensure_instance_settings(api: GreenAPI) -> bool:
    """Выставляет нужные настройки инстанса, только если они отличаются. True — настройки менялись."""
    current = await api.get_settings()
    diff = {
        key: value
        for key, value in DESIRED_SETTINGS.items()
        if str(current.get(key) if current.get(key) is not None else "") != str(value)
    }
    if not diff:
        return False
    log.info("Меняю настройки инстанса: %s", ", ".join(diff))
    await api.set_settings(diff)
    return True


class Watchdog:
    def __init__(
        self,
        api: GreenAPI,
        db: Database,
        engine: Engine,
        notifier: Notifier,
        runtime: Runtime,
        *,
        clock=time.time,
        poll_seconds: float = 60.0,
    ):
        self.api = api
        self.db = db
        self.engine = engine
        self.notifier = notifier
        self.runtime = runtime
        self.clock = clock
        self.poll_seconds = poll_seconds
        self._starting_since: float | None = None
        self._rebooted = False
        self._creds_alerted = False

    async def run_state(self) -> None:
        while True:
            try:
                await self._poll_state()
            except Exception:
                # Сторож не должен молча умирать: иначе бан или отвязка номера останутся незамеченными.
                log.exception("Сбой сторожа номера, продолжаю опрос")
            await asyncio.sleep(self.poll_seconds)

    async def _poll_state(self) -> None:
        try:
            state = await self.api.get_state()
        except GreenAPIError as exc:
            log.warning("Не удалось получить состояние номера: %s", exc)
            if exc.bad_credentials and not self._creds_alerted:
                self._creds_alerted = True
                await self.notifier.owner("⛔ GREEN-API не принял idInstance или токен. Проверьте .env.")
        else:
            self._creds_alerted = False
            await self.apply_state(state)

    async def apply_state(self, state: str) -> None:
        old = self.runtime.wa_state
        await self._watch_starting(state)
        if state == old:
            return
        self.runtime.wa_state = state
        self.runtime.wa_state_since = self.clock()
        log.info("Состояние номера: %s → %s", old, state)

        if state in ("notAuthorized", "blocked", "suspended", "yellowCard"):
            try:
                await self.api.clear_queue()  # иначе накопленное уйдёт залпом после переподключения
            except GreenAPIError as exc:
                log.warning("Не удалось очистить очередь: %s", exc)
        if state in ("blocked", "suspended", "yellowCard"):
            await self.engine.stop()

        if state == "authorized":
            if await self.db.get("running") == "1" and old != "unknown":
                await self.engine.reschedule()
                self.engine.wake()
            if old == "unknown":
                return  # обычный старт, сообщать нечего

        if state == "starting":
            return  # сообщаем, только если запуск затянулся (см. _watch_starting)

        text = STATE_ALERT.get(state)
        if text:
            await self.notifier.admins(text)

    async def _watch_starting(self, state: str) -> None:
        if state != "starting":
            self._starting_since = None
            self._rebooted = False
            return
        now = self.clock()
        if self._starting_since is None:
            self._starting_since = now
            return
        if now - self._starting_since > 300 and not self._rebooted:
            self._rebooted = True
            try:
                await self.api.reboot()
            except GreenAPIError as exc:
                log.warning("Не удалось перезапустить инстанс: %s", exc)
            await self.notifier.owner(
                "⏳ Инстанс GREEN-API запускается дольше 5 минут. Отправил reboot. "
                "Если не поможет — напишите в поддержку GREEN-API."
            )

    async def run_notifications(self) -> None:
        while True:
            try:
                item = await self.api.receive_notification(20)
            except GreenAPIError as exc:
                await asyncio.sleep(10 if exc.network else 30)
                continue
            except Exception:
                log.exception("Сбой при получении уведомлений GREEN-API, повтор через 30 с")
                await asyncio.sleep(30)
                continue
            if not item:
                continue
            receipt_id = item.get("receiptId")
            try:
                await self.handle(item.get("body") or {})
            except Exception:
                log.exception("Не удалось обработать уведомление GREEN-API")
            if receipt_id is not None:
                try:
                    await self.api.delete_notification(receipt_id)
                except GreenAPIError as exc:
                    log.warning("Не удалось удалить уведомление %s: %s", receipt_id, exc)

    async def handle(self, body: dict) -> None:
        kind = body.get("typeWebhook")
        if kind == "outgoingMessageStatus":
            id_message, status = body.get("idMessage"), body.get("status")
            if not id_message or not status:
                return
            chat_id = await self.db.log_status_by_message(str(id_message), status)
            if not chat_id:
                return
            if status == "notInGroup":
                await self.db.set_group_status(chat_id, "removed")
                group = await self.db.get_group(chat_id)
                name = group["name"] if group else chat_id
                await self.notifier.admins(f"⚠️ Номер не состоит в группе «{name}». Группа выключена из рассылки.")
            elif status == "failed":
                await self.engine.register_delivery_failure(chat_id)
        elif kind == "stateInstanceChanged":
            state = body.get("stateInstance")
            if state:
                await self.apply_state(state)
