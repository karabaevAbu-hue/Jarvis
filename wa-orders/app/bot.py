"""Telegram-пульт: заказы дня, группы, запуск, статус, настройки."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime
from logging import Handler

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .config import LOG_DIR, Config
from .db import Database
from .engine import Engine, Runtime, day_start, in_quiet_hours, parse_hhmm
from .greenapi import GreenAPI, GreenAPIError
from .textfmt import build_summary, split_orders, tg_html_to_wa
from .watchdog import STATE_RU

log = logging.getLogger(__name__)

BTN_ORDERS = "📦 Заказы"
BTN_GROUPS = "👥 Группы"
BTN_START = "▶️ Запустить"
BTN_PAUSE = "⏸ Пауза"
BTN_STATUS = "📊 Статус"
BTN_TEST = "🧪 Тест-отправка"
BTN_STOP_ALL = "🛑 Стоп всё"
BTN_SETTINGS = "⚙️ Настройки"

PAGE_SIZE = 8

_background: set[asyncio.Task] = set()  # держим ссылки, иначе фоновую задачу может собрать сборщик мусора


class Notifier:
    def __init__(self, bot: Bot, owner_id: int, admin_ids: frozenset[int]):
        self.bot = bot
        self.owner_id = owner_id
        self.admin_ids = admin_ids

    async def owner(self, text: str) -> None:
        await self._send(self.owner_id, text)

    async def admins(self, text: str) -> None:
        for user_id in sorted(self.admin_ids):
            await self._send(user_id, text)

    async def _send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as exc:  # уведомление не должно ронять движок
            log.warning("Не удалось отправить уведомление %s: %s", user_id, exc)


class AdminFilter(Filter):
    def __init__(self, ids: frozenset[int]):
        self.ids = ids

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and user.id in self.ids)


class OrderInput(StatesGroup):
    one = State()
    all = State()


@dataclass
class Deps:
    db: Database
    api: GreenAPI
    engine: Engine
    cfg: Config
    runtime: Runtime
    log_handler: Handler | None = None


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_GROUPS)],
            [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_PAUSE)],
            [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_TEST)],
            [KeyboardButton(text=BTN_STOP_ALL), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
    )


HELP = (
    "Пульт рассылки заказов в группы WhatsApp.\n\n"
    "1. «👥 Группы» → «🔄 Обновить из WhatsApp» → включите нужные группы.\n"
    "2. «📦 Заказы» → заполните до 3 заказов. Они уйдут одной сводкой.\n"
    "3. «🧪 Тест-отправка» — проверить, как сводка выглядит в WhatsApp.\n"
    "4. «▶️ Запустить» — рассылка по расписанию.\n\n"
    "«⏸ Пауза» останавливает расписание, «🛑 Стоп всё» — ещё и чистит очередь GREEN-API.\n"
    "/cancel — отменить ввод."
)


def build_router(deps: Deps) -> Router:
    router = Router(name="panel")
    admin = AdminFilter(deps.cfg.admin_ids)
    router.message.filter(admin)
    router.callback_query.filter(admin)
    db, api, engine, cfg, runtime = deps.db, deps.api, deps.engine, deps.cfg, deps.runtime

    def is_owner(user_id: int) -> bool:
        return user_id == cfg.owner_id

    def fmt_time(ts: float) -> str:
        return datetime.fromtimestamp(ts, cfg.tz).strftime("%H:%M")

    async def summary_with(orders_by_slot: dict[int, str]) -> str:
        headers = [await db.get(f"header_{i}") for i in (1, 2, 3)]
        longest = max(headers, key=len) if headers else ""
        texts = [orders_by_slot[s] for s in sorted(orders_by_slot)]
        return build_summary(texts, longest, await db.get("footer"))

    async def active_by_slot() -> dict[int, str]:
        return {o["slot"]: o["text"] for o in await db.active_orders()}

    # --- заказы ---
    async def orders_view() -> tuple[str, InlineKeyboardMarkup]:
        current = await active_by_slot()
        lines = ["📦 Заказы дня — уходят в группы одной сводкой.", ""]
        for slot in (1, 2, 3):
            text = current.get(slot)
            if text:
                first = text.splitlines()[0]
                more = "…" if len(first) > 60 or "\n" in text else ""
                lines.append(f"№{slot}: {first[:60]}{more}")
            else:
                lines.append(f"№{slot}: — пусто —")
        preview = await summary_with(current) if current else ""
        lines += ["", f"Предпросмотр ({len(preview)} симв.; *звёздочки* в WhatsApp — жирный):", "", preview or "—"]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✏️ №{s}", callback_data=f"o:set:{s}") for s in (1, 2, 3)],
                [InlineKeyboardButton(text=f"✖️ Снять №{s}", callback_data=f"o:del:{s}") for s in (1, 2, 3)],
                [InlineKeyboardButton(text="📋 Вставить все три", callback_data="o:all")],
            ]
        )
        return "\n".join(lines), keyboard

    async def check_length(orders_by_slot: dict[int, str]) -> str | None:
        summary = await summary_with(orders_by_slot)
        limit = await db.get_int("max_chars")
        if len(summary) > limit:
            return (
                f"Сводка получится {len(summary)} символов, а максимум {limit}: иначе 2-й и 3-й заказ "
                "спрячутся под «Читать далее». Сократите текст и пришлите снова."
            )
        return None

    @router.message(Command("start", "help", "menu"))
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(HELP, reply_markup=main_menu())

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())

    @router.message(F.text == BTN_ORDERS)
    async def show_orders(message: Message, state: FSMContext):
        await state.clear()
        text, keyboard = await orders_view()
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("o:set:"))
    async def order_set(callback: CallbackQuery, state: FSMContext):
        slot = int(callback.data.split(":")[2])
        await state.set_state(OrderInput.one)
        await state.update_data(slot=slot)
        await callback.message.answer(f"Пришлите текст заказа №{slot} одним сообщением.\n/cancel — отмена.")
        await callback.answer()

    @router.callback_query(F.data == "o:all")
    async def order_all(callback: CallbackQuery, state: FSMContext):
        await state.set_state(OrderInput.all)
        await callback.message.answer(
            "Пришлите до трёх заказов одним сообщением, между заказами — пустая строка.\n/cancel — отмена."
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("o:del:"))
    async def order_del(callback: CallbackQuery):
        slot = int(callback.data.split(":")[2])
        await db.remove_order(slot)
        text, keyboard = await orders_view()
        await _edit(callback, text, keyboard)
        await callback.answer(f"Заказ №{slot} снят. Со следующего сообщения его не будет.")

    def _order_text(message: Message) -> str:
        if not (message.text or message.caption):
            return ""
        return tg_html_to_wa(message.html_text)

    @router.message(OrderInput.one)
    async def order_one_input(message: Message, state: FSMContext):
        text = _order_text(message)
        if not text:
            await message.answer("Нужен текст заказа. Пришлите его сообщением или /cancel.")
            return
        slot = (await state.get_data())["slot"]
        current = await active_by_slot()
        current[slot] = text
        problem = await check_length(current)
        if problem:
            await message.answer(problem)
            return
        await db.set_order(slot, text, message.from_user.id)
        await state.clear()
        note = "\nФото пока не отправляем, взял только текст." if message.photo else ""
        view, keyboard = await orders_view()
        await message.answer(f"Заказ №{slot} сохранён.{note}\n\n{view}", reply_markup=keyboard)

    @router.message(OrderInput.all)
    async def order_all_input(message: Message, state: FSMContext):
        blocks = split_orders(_order_text(message))
        if not blocks:
            await message.answer("Нужен текст. Пришлите заказы или /cancel.")
            return
        if len(blocks) > 3:
            await message.answer(f"Нашёл {len(blocks)} заказов, а слотов 3. Уберите лишнее или пустые строки внутри заказа.")
            return
        new = {i: b for i, b in enumerate(blocks, start=1)}
        problem = await check_length(new)
        if problem:
            await message.answer(problem)
            return
        for slot in (1, 2, 3):
            if slot in new:
                await db.set_order(slot, new[slot], message.from_user.id)
            else:
                await db.remove_order(slot)
        await state.clear()
        view, keyboard = await orders_view()
        await message.answer(f"Сохранено заказов: {len(blocks)}.\n\n{view}", reply_markup=keyboard)

    # --- группы ---
    async def groups_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
        groups = await db.list_groups()
        pages = max(1, math.ceil(len(groups) / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        enabled = sum(1 for g in groups if g["enabled"])
        live, quiet = await db.get_int("live_interval"), await db.get_int("quiet_interval")
        limit = f" из {cfg.max_groups} (тариф Developer)" if cfg.max_groups else ""
        lines = [
            f"👥 Группы WhatsApp: {len(groups)}, включено {enabled}{limit}.",
            f"✅ включена · Ж — живая, раз в {live} мин · Т — тихая, раз в {quiet} мин",
            "❌ номер не в группе · ⚠️ выключена после ошибок",
        ]
        if not groups:
            lines += ["", "Список пуст. Нажмите «🔄 Обновить из WhatsApp»."]
        rows = [[InlineKeyboardButton(text="🔄 Обновить из WhatsApp", callback_data=f"g:sync:{page}")]]
        for group in groups[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
            if group["status"] == "removed":
                mark = "❌"
            elif group["status"] == "error":
                mark = "⚠️"
            else:
                mark = "✅" if group["enabled"] else "⬜"
            rows.append(
                [
                    InlineKeyboardButton(text=f"{mark} {group['name'][:30]}", callback_data=f"g:t:{page}:{group['chat_id']}"),
                    InlineKeyboardButton(
                        text="Ж" if group["kind"] == "live" else "Т", callback_data=f"g:k:{page}:{group['chat_id']}"
                    ),
                ]
            )
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="◀️", callback_data=f"g:p:{page - 1}"))
            nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"g:p:{page}"))
            if page < pages - 1:
                nav.append(InlineKeyboardButton(text="▶️", callback_data=f"g:p:{page + 1}"))
            rows.append(nav)
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    @router.message(F.text == BTN_GROUPS)
    async def show_groups(message: Message, state: FSMContext):
        await state.clear()
        text, keyboard = await groups_view(0)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("g:p:"))
    async def groups_page(callback: CallbackQuery):
        text, keyboard = await groups_view(int(callback.data.split(":")[2]))
        await _edit(callback, text, keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("g:sync:"))
    async def groups_sync(callback: CallbackQuery):
        page = int(callback.data.split(":")[2])
        try:
            found = await api.get_groups()
        except GreenAPIError as exc:
            await callback.answer(f"GREEN-API не ответил: {exc}"[:190], show_alert=True)
            return
        new, gone = await db.sync_groups([(g["id"], g.get("name") or g["id"]) for g in found])
        text, keyboard = await groups_view(page)
        await _edit(callback, text, keyboard)
        await callback.answer(f"Групп: {len(found)}, новых {new}, пропало {gone}")

    @router.callback_query(F.data.startswith("g:t:"))
    async def group_toggle(callback: CallbackQuery):
        _, _, page, chat_id = callback.data.split(":", 3)
        group = await db.get_group(chat_id)
        if not group:
            await callback.answer("Группа не найдена, обновите список.", show_alert=True)
            return
        if group["enabled"]:
            await db.set_group_enabled(chat_id, False)
            note = "Группа выключена."
        else:
            if group["status"] == "removed":
                await callback.answer("Номер не состоит в этой группе.", show_alert=True)
                return
            if cfg.max_groups and await db.enabled_count() >= cfg.max_groups:
                await callback.answer(
                    f"На бесплатном тарифе Developer можно включить только {cfg.max_groups} группы.", show_alert=True
                )
                return
            next_send = None
            if await db.get("running") == "1":
                next_send = time.time() + random.uniform(30, 120)  # подключается к идущей рассылке без залпа
            await db.set_group_enabled(chat_id, True, next_send)
            note = "Группа включена."
        text, keyboard = await groups_view(int(page))
        await _edit(callback, text, keyboard)
        await callback.answer(note)

    @router.callback_query(F.data.startswith("g:k:"))
    async def group_kind(callback: CallbackQuery):
        _, _, page, chat_id = callback.data.split(":", 3)
        group = await db.get_group(chat_id)
        if not group:
            await callback.answer("Группа не найдена.", show_alert=True)
            return
        kind = "quiet" if group["kind"] == "live" else "live"
        await db.set_group_kind(chat_id, kind)
        text, keyboard = await groups_view(int(page))
        await _edit(callback, text, keyboard)
        await callback.answer("Теперь тихая." if kind == "quiet" else "Теперь живая.")

    # --- запуск и остановка ---
    @router.message(F.text == BTN_START)
    async def start_sending(message: Message, state: FSMContext):
        await state.clear()
        if runtime.wa_state != "authorized":
            await message.answer(
                f"Номер сейчас: {STATE_RU.get(runtime.wa_state, runtime.wa_state)}.\n"
                "Запуск возможен, когда номер в сети."
            )
            return
        groups = await db.enabled_groups()
        if not groups:
            await message.answer("Включите хотя бы одну группу в «👥 Группы».")
            return
        orders = await db.active_orders()
        if not orders:
            await message.answer("Добавьте заказы в «📦 Заказы».")
            return
        plan = await engine.start()
        lines = [
            f"▶️ Рассылка запущена: {len(groups)} групп, сводка из {len(orders)} заказов.",
            f"По плану {plan.per_hour:.0f} сообщ./час, номер успевает до {plan.capacity:.0f}.",
            "Первые сообщения разнесены по интервалу, без залпа.",
        ]
        if plan.factor > 1:
            lines.append(f"⚠️ Не успеваем: реальные интервалы будут примерно в {plan.factor:.1f} раза длиннее.")
        if not (await db.get("footer")).strip():
            lines.append("Подсказка: телефон для связи не задан — /set footer 📞 +996 ...")
        await message.answer("\n".join(lines))

    @router.message(F.text == BTN_PAUSE)
    async def pause_sending(message: Message, state: FSMContext):
        await state.clear()
        await engine.stop()
        await message.answer("⏸ Рассылка на паузе. Очередь GREEN-API не трогали — «🛑 Стоп всё» очистит и её.")

    @router.message(F.text == BTN_STOP_ALL)
    async def stop_all(message: Message, state: FSMContext):
        await state.clear()
        left = await engine.stop_all()
        if left == 0:
            queue = "пуста"
        elif left is None:
            queue = "проверить не удалось"
        else:
            queue = f"осталось {left}"
        await message.answer(f"🛑 Всё остановлено. Очередь GREEN-API: {queue}.")

    # --- тест ---
    @router.message(F.text == BTN_TEST)
    async def test_ask(message: Message, state: FSMContext):
        await state.clear()
        groups = await db.enabled_groups()
        if not groups or not await db.active_orders():
            await message.answer("Для теста нужны включённые группы и хотя бы один заказ.")
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Отправить", callback_data="t:go"),
                    InlineKeyboardButton(text="Отмена", callback_data="t:no"),
                ]
            ]
        )
        names = ", ".join(g["name"] for g in groups)
        await message.answer(f"Отправить сводку сейчас в {len(groups)} групп? ({names})", reply_markup=keyboard)

    @router.callback_query(F.data == "t:no")
    async def test_cancel(callback: CallbackQuery):
        await _edit(callback, "Тест отменён.", None)
        await callback.answer()

    @router.callback_query(F.data == "t:go")
    async def test_go(callback: CallbackQuery):
        await _edit(callback, "🧪 Отправляю…", None)
        await callback.answer()
        chat_id = callback.message.chat.id
        bot = callback.bot

        async def _run():
            results = await engine.send_test()
            lines = ["🧪 Итог теста:"] + [
                f"{'✅' if ok else '❌'} {name}" + (f" — {err[:120]}" if err else "") for name, ok, err in results
            ]
            await bot.send_message(chat_id, "\n".join(lines) if results else "Отправлять было некуда.")

        task = asyncio.create_task(_run())
        _background.add(task)
        task.add_done_callback(_background.discard)

    # --- статус ---
    @router.message(F.text == BTN_STATUS)
    async def status(message: Message, state: FSMContext):
        await state.clear()
        now = time.time()
        running = await db.get("running") == "1"
        groups = await db.enabled_groups()
        sent = await db.sent_since(day_start(now, cfg.tz))
        limit = await db.get_int("daily_limit")
        next_time = await db.next_due_time()
        try:
            queue = str(len(await api.show_queue()))
        except GreenAPIError:
            queue = "?"
        plan = await engine.plan()
        qs, qe = await db.get("quiet_start"), await db.get("quiet_end")
        quiet_now = in_quiet_hours(now, cfg.tz, qs, qe)
        group_limit = f" из {cfg.max_groups}" if cfg.max_groups else ""
        lines = [
            "📊 Статус",
            f"Номер: {STATE_RU.get(runtime.wa_state, runtime.wa_state)}",
            f"Рассылка: {'идёт' if running else 'на паузе'}{' (сейчас тихие часы)' if running and quiet_now else ''}",
            f"Групп включено: {len(groups)}{group_limit}",
            f"Отправлено сегодня: {sent} из {limit}",
            f"Очередь GREEN-API: {queue}",
        ]
        if running and next_time:
            lines.append(f"Следующая отправка: {fmt_time(max(next_time, now))}")
        lines += [
            f"План: {plan.per_hour:.0f} сообщ./час, номер успевает до {plan.capacity:.0f}",
            f"Тихие часы: {qs}–{qe}, пауза {await db.get('pause_min')}–{await db.get('pause_max')} с",
        ]
        await message.answer("\n".join(lines))

    # --- настройки ---
    @router.message(F.text == BTN_SETTINGS)
    async def settings_view(message: Message, state: FSMContext):
        await state.clear()
        g = db.get
        lines = [
            "⚙️ Настройки (меняет только владелец)",
            f"Живые группы: раз в {await g('live_interval')} мин — /set live 15",
            f"Тихие группы: раз в {await g('quiet_interval')} мин — /set quiet 45",
            f"Пауза между сообщениями: {await g('pause_min')}–{await g('pause_max')} с — /set pause 20 30",
            f"Тихие часы: {await g('quiet_start')}–{await g('quiet_end')} — /set night 23:00 07:00",
            f"Лимит в сутки: {await g('daily_limit')} — /set limit 200",
            f"Шапка 1: {await g('header_1') or '—'} — /set header1 текст",
            f"Шапка 2: {await g('header_2') or '—'} — /set header2 текст",
            f"Шапка 3: {await g('header_3') or '—'} — /set header3 текст",
            f"Подвал: {await g('footer') or 'не задан'} — /set footer 📞 +996 ...",
            "Чтобы убрать шапку или подвал: /set header2 -",
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("set"))
    async def cmd_set(message: Message, command: CommandObject):
        if not is_owner(message.from_user.id):
            await message.answer("Настройки меняет только владелец.")
            return
        args = (command.args or "").strip()
        key, _, rest = args.partition(" ")
        key, rest = key.lower(), rest.strip()
        try:
            reply = await _apply_setting(key, rest)
        except ValueError as exc:
            await message.answer(f"Не получилось: {exc}")
            return
        await message.answer(f"✅ {reply}")

    async def _apply_setting(key: str, value: str) -> str:
        def as_int(raw: str, low: int, high: int, what: str) -> int:
            try:
                number = int(raw)
            except ValueError:
                raise ValueError(f"{what} — нужно число") from None
            if not low <= number <= high:
                raise ValueError(f"{what} — от {low} до {high}")
            return number

        if key in ("live", "quiet"):
            minutes = as_int(value, 5, 240, "интервал")
            await db.set(f"{key}_interval", minutes)
            return f"{'Живые' if key == 'live' else 'Тихие'} группы: раз в {minutes} мин"
        if key == "pause":
            parts = value.split()
            if len(parts) != 2:
                raise ValueError("формат: /set pause 20 30")
            low = as_int(parts[0], 10, 300, "пауза")  # не меньше задержки на стороне GREEN-API (10 с)
            high = as_int(parts[1], 10, 300, "пауза")
            if low > high:
                raise ValueError("первое число должно быть не больше второго")
            await db.set("pause_min", low)
            await db.set("pause_max", high)
            return f"Пауза между сообщениями: {low}–{high} с"
        if key == "night":
            parts = value.split()
            if len(parts) != 2 or not all(parse_hhmm(p) for p in parts):
                raise ValueError("формат: /set night 23:00 07:00")
            await db.set("quiet_start", parts[0])
            await db.set("quiet_end", parts[1])
            return f"Тихие часы: {parts[0]}–{parts[1]}"
        if key == "limit":
            limit = as_int(value, 1, 10000, "лимит")
            await db.set("daily_limit", limit)
            return f"Лимит в сутки: {limit}"
        if key in ("header1", "header2", "header3", "footer"):
            text = "" if value == "-" else value
            if key == "footer" and len(text) > 200:
                raise ValueError("подвал длиннее 200 символов")
            if key != "footer" and len(text) > 80:
                raise ValueError("шапка длиннее 80 символов")
            db_key = "footer" if key == "footer" else f"header_{key[-1]}"
            await db.set(db_key, text)
            return f"{'Подвал' if key == 'footer' else 'Шапка ' + key[-1]}: {text or 'убрано'}"
        raise ValueError("неизвестная настройка. Список — кнопка «⚙️ Настройки»")

    @router.message(Command("debug"))
    async def cmd_debug(message: Message):
        if not is_owner(message.from_user.id):
            return
        path = LOG_DIR / "bot.log"
        if not path.exists():
            await message.answer("Лог пока пуст.")
            return
        handler = deps.log_handler
        if handler:
            handler.acquire()
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 1_500_000))
                data = fh.read()
        finally:
            if handler:
                handler.release()
        await message.answer_document(BufferedInputFile(data, filename="bot.log"), caption="Последняя часть лога")

    @router.message()
    async def fallback(message: Message):
        await message.answer("Не понял. Пользуйтесь кнопками меню или /help.", reply_markup=main_menu())

    return router


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
