"""Движок рассылки: сводка заказов дня в группы по расписанию."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Protocol
from zoneinfo import ZoneInfo

from .db import Database
from .greenapi import GreenAPI, GreenAPIError
from .textfmt import build_summary, pick_header

log = logging.getLogger(__name__)

REQUEST_OVERHEAD_S = 2.0  # время самого запроса к GREEN-API
CAPACITY_RESERVE = 0.9  # запас 10%
FAILS_PER_GROUP = 3
FAILS_GLOBAL = 5


class Notifier(Protocol):
    async def owner(self, text: str) -> None: ...

    async def admins(self, text: str) -> None: ...


@dataclass
class Runtime:
    wa_state: str = "unknown"
    wa_state_since: float = field(default_factory=time.time)


def parse_hhmm(value: str) -> dtime | None:
    try:
        hours, minutes = value.strip().split(":")
        return dtime(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(now: float, tz: ZoneInfo, start: str, end: str) -> bool:
    t_start, t_end = parse_hhmm(start), parse_hhmm(end)
    if t_start is None or t_end is None or t_start == t_end:
        return False
    current = datetime.fromtimestamp(now, tz).time()
    if t_start < t_end:
        return t_start <= current < t_end
    return current >= t_start or current < t_end  # через полночь, например 23:00–07:00


def day_start(now: float, tz: ZoneInfo) -> float:
    local = datetime.fromtimestamp(now, tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass(frozen=True)
class Plan:
    per_hour: float  # сколько сообщений в час нужно по плану
    capacity: float  # сколько номер успевает при текущих паузах
    factor: float  # во сколько раз растянутся интервалы, если не успеваем (1.0 — успеваем)


def make_plan(intervals_min: list[int], pause_min: int, pause_max: int) -> Plan:
    per_hour = sum(60 / iv for iv in intervals_min if iv > 0)
    step = (pause_min + pause_max) / 2 + REQUEST_OVERHEAD_S
    capacity = CAPACITY_RESERVE * 3600 / step
    factor = per_hour / capacity if per_hour > capacity else 1.0
    return Plan(per_hour, capacity, factor)


def _text_of(item: dict) -> str:
    return (item.get("textMessage") or (item.get("extendedTextMessage") or {}).get("text") or "").strip()


class Engine:
    def __init__(
        self,
        db: Database,
        api: GreenAPI,
        notifier: Notifier,
        runtime: Runtime,
        tz: ZoneInfo,
        *,
        clock=time.time,
        rng: random.Random | None = None,
        verify_delay: float = 60.0,
    ):
        self.db = db
        self.api = api
        self.notifier = notifier
        self.runtime = runtime
        self.tz = tz
        self.clock = clock
        self.rng = rng or random.Random()
        self.verify_delay = verify_delay
        self._wake = asyncio.Event()
        self._global_fail = 0
        self._notified: dict[str, float] = {}
        self._background: set[asyncio.Task] = set()

    # --- управление ---
    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> Plan:
        await self.db.set("running", "1")
        self._global_fail = 0
        await self.reschedule()
        self.wake()
        return await self.plan()

    async def stop(self) -> None:
        await self.db.set("running", "0")
        await self.db.clear_schedule()
        self.wake()

    async def stop_all(self) -> int | None:
        """Пауза + очистка очереди GREEN-API. Возвращает остаток очереди (None — не удалось проверить)."""
        await self.stop()
        try:
            await self.api.clear_queue()
            return len(await self.api.show_queue())
        except GreenAPIError as exc:
            log.warning("Не удалось очистить очередь GREEN-API: %s", exc)
            return None

    async def reschedule(self) -> None:
        """Первые сообщения разносятся по интервалу: группа №i уходит в момент старт + i × интервал / N."""
        now = self.clock()
        groups = await self.db.enabled_groups()
        count = len(groups)
        for index, group in enumerate(groups):
            interval = await self.group_interval(group)
            await self.db.set_next_send(group["chat_id"], now + index * interval * 60 / count)

    async def group_interval(self, group) -> int:
        if group["interval_min"]:
            return int(group["interval_min"])
        key = "quiet_interval" if group["kind"] == "quiet" else "live_interval"
        return await self.db.get_int(key)

    async def plan(self) -> Plan:
        groups = await self.db.enabled_groups()
        intervals = [await self.group_interval(g) for g in groups]
        return make_plan(intervals, await self.db.get_int("pause_min"), await self.db.get_int("pause_max"))

    # --- главный цикл ---
    async def run(self) -> None:
        stale = await self.db.mark_stale_sending()
        if stale:
            log.warning("После перезапуска %s записей «отправляется» помечены unknown, повторно не шлём", stale)
        if await self.db.get("running") == "1":
            await self.reschedule()  # без залпа после перезагрузки
        while True:
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Сбой в движке рассылки")
                delay = 10.0
            await self._sleep(delay)

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def step(self) -> float:
        """Один шаг: отправить одну созревшую группу. Возвращает, сколько секунд ждать до следующего шага."""
        if await self.db.get("running") != "1":
            return 10.0
        if self.runtime.wa_state != "authorized":
            return 10.0

        now = self.clock()
        if in_quiet_hours(now, self.tz, await self.db.get("quiet_start"), await self.db.get("quiet_end")):
            return 60.0

        limit = await self.db.get_int("daily_limit")
        if await self.db.sent_since(day_start(now, self.tz)) >= limit:
            await self._notify_once("limit", f"Дневной лимит {limit} сообщений исчерпан. Рассылка продолжится завтра.")
            return 60.0

        group = await self.db.due_group(now)
        if group is None:
            next_time = await self.db.next_due_time()
            return 10.0 if next_time is None else min(10.0, max(1.0, next_time - now))

        orders = await self.db.active_orders()
        if not orders:
            await self._notify_once("no_orders", "Нет активных заказов — рассылка ждёт. Добавьте их в «📦 Заказы».")
            return 30.0

        headers = [await self.db.get(f"header_{i}") for i in (1, 2, 3)]
        header_index, header = pick_header(headers, group["last_header"], self.rng)
        text = build_summary([o["text"] for o in orders], header, await self.db.get("footer"))

        chat_id = group["chat_id"]
        interval = await self.group_interval(group)
        log_id = await self.db.log_sending(chat_id, group["next_send_at"], text)
        # Следующий слот сохраняем ДО отправки: если связь оборвётся, дубля не будет.
        # Случайная прибавка только в плюс (до +15%): интервал означает «не чаще, чем».
        next_send = now + interval * 60 * self.rng.uniform(1.0, 1.15)
        await self.db.set_next_send(chat_id, next_send, last_header=header_index)

        try:
            id_message = await self.api.send_message(chat_id, text)
        except GreenAPIError as exc:
            await self._on_send_error(group, log_id, text, exc)
        else:
            await self.db.log_update(log_id, "queued", id_message=id_message)
            if group["fail_streak"]:
                await self.db.reset_fail(chat_id)
            self._global_fail = 0
            log.info("Сводка ушла в очередь: %s", group["name"])

        return self.rng.uniform(await self.db.get_int("pause_min"), await self.db.get_int("pause_max"))

    # --- ошибки ---
    async def _on_send_error(self, group, log_id: int, text: str, exc: GreenAPIError) -> None:
        chat_id, name = group["chat_id"], group["name"]
        log.warning("Отправка в «%s» не удалась: %s", name, exc)

        if exc.maybe_sent:
            # Ответа нет, но сообщение могло уйти. Проверяем журнал через минуту, повторяем только если не ушло.
            await self.db.log_update(log_id, "unknown", error=str(exc))
            task = asyncio.create_task(self._verify_later(log_id, chat_id, text))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            await self._count_global_fail()
            return

        await self.db.log_update(log_id, "error", error=str(exc))
        if exc.rate_limited:
            await self.db.set_next_send(chat_id, self.clock() + 15)
            return
        if exc.quota:
            await self.db.set("running", "0")
            self.wake()
            await self.notifier.admins(
                "⛔ GREEN-API: исчерпан лимит тарифа (на Developer можно только 3 чата). Рассылка остановлена."
            )
            return
        if exc.bad_credentials:
            await self.db.set("running", "0")
            self.wake()
            await self.notifier.owner("⛔ GREEN-API не принял idInstance или токен. Проверьте .env. Рассылка остановлена.")
            return
        await self._group_failed(chat_id, name)

    async def register_delivery_failure(self, chat_id: str) -> None:
        """Статус failed пришёл из уведомлений GREEN-API."""
        group = await self.db.get_group(chat_id)
        await self._group_failed(chat_id, group["name"] if group else chat_id)

    async def _group_failed(self, chat_id: str, name: str) -> None:
        streak = await self.db.inc_fail(chat_id)
        if streak >= FAILS_PER_GROUP:
            await self.db.set_group_status(chat_id, "error")
            await self.notifier.admins(
                f"⚠️ Группа «{name}»: {FAILS_PER_GROUP} ошибки подряд, отправка в неё выключена. "
                "Проверьте, может ли номер писать в группу, и включите её снова в «👥 Группы»."
            )
        await self._count_global_fail()

    async def _count_global_fail(self) -> None:
        self._global_fail += 1
        if self._global_fail < FAILS_GLOBAL:
            return
        self._global_fail = 0
        await self.stop()
        try:
            await self.api.clear_queue()
        except GreenAPIError:
            pass
        await self.notifier.admins(
            f"🛑 Автопауза: {FAILS_GLOBAL} ошибок подряд. Так часто выглядит начало бана. "
            "Проверьте номер и запустите рассылку вручную."
        )

    async def _verify_later(self, log_id: int, chat_id: str, text: str) -> None:
        await asyncio.sleep(self.verify_delay)
        try:
            items = await self.api.last_outgoing(minutes=15)
        except GreenAPIError as exc:
            log.warning("Не удалось проверить журнал исходящих: %s. Повтор не делаем, чтобы не было дубля", exc)
            return
        for item in items:
            if item.get("chatId") == chat_id and _text_of(item) == text.strip():
                await self.db.log_update(log_id, "queued", id_message=str(item.get("idMessage", "")))
                return
        await self.db.log_update(log_id, "not_sent")
        if await self.db.get("running") == "1":
            await self.db.set_next_send(chat_id, self.clock())  # один повтор
            self.wake()

    async def _notify_once(self, key: str, text: str) -> None:
        today = day_start(self.clock(), self.tz)
        if self._notified.get(key) == today:
            return
        self._notified[key] = today
        await self.notifier.admins(text)

    # --- тест ---
    async def send_test(self) -> list[tuple[str, bool, str]]:
        """Отправить сводку сейчас во все включённые группы (с паузой 5–8 с). Расписание не трогает."""
        groups = await self.db.enabled_groups()
        orders = await self.db.active_orders()
        if not groups or not orders:
            return []
        headers = [await self.db.get(f"header_{i}") for i in (1, 2, 3)]
        footer = await self.db.get("footer")
        results: list[tuple[str, bool, str]] = []
        for index, group in enumerate(groups):
            if index:
                await asyncio.sleep(self.rng.uniform(5, 8))
            _, header = pick_header(headers, group["last_header"], self.rng)
            text = build_summary([o["text"] for o in orders], header, footer)
            log_id = await self.db.log_sending(group["chat_id"], None, text, kind="test")
            try:
                id_message = await self.api.send_message(group["chat_id"], text)
            except GreenAPIError as exc:
                await self.db.log_update(log_id, "error", error=str(exc))
                results.append((group["name"], False, str(exc)))
            else:
                await self.db.log_update(log_id, "queued", id_message=id_message)
                results.append((group["name"], True, ""))
        return results
