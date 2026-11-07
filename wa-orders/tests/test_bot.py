"""Прогон Telegram-пульта через настоящий Dispatcher aiogram с поддельной сессией Telegram."""
from __future__ import annotations

import asyncio
import itertools
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot import (
    BTN_GROUPS,
    BTN_ORDERS,
    BTN_PAUSE,
    BTN_SETTINGS,
    BTN_START,
    BTN_STATUS,
    BTN_STOP_ALL,
    BTN_TEST,
    Deps,
    build_router,
)
from app.config import Config

OWNER = 1001
OPERATOR = 1002
STRANGER = 9999


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls), date=datetime.now(), chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    async def close(self):
        pass

    def texts(self):
        out = []
        for call in self.calls:
            if isinstance(call, (SendMessage, EditMessageText)):
                out.append(call.text)
            elif isinstance(call, AnswerCallbackQuery) and call.text:
                out.append(call.text)
        return out


_ids = itertools.count(1)


def user(uid):
    return User(id=uid, is_bot=False, first_name="Abu")


def text_update(uid, text):
    n = next(_ids)
    return Update(
        update_id=n,
        message=Message(message_id=n, date=datetime.now(), chat=Chat(id=uid, type="private"), from_user=user(uid), text=text),
    )


def callback_update(uid, data):
    n = next(_ids)
    msg = Message(message_id=n, date=datetime.now(), chat=Chat(id=uid, type="private"), text="старое")
    return Update(
        update_id=n,
        callback_query=CallbackQuery(id=str(n), from_user=user(uid), chat_instance="ci", data=data, message=msg),
    )


@pytest.fixture
def panel(db, api, engine, runtime, tmp_path):
    cfg = Config(
        tg_token="123456:" + "A" * 35,
        owner_id=OWNER,
        operator_ids=frozenset({OPERATOR}),
        green_api_url="https://x",
        green_media_url="https://y",
        green_id_instance="1",
        green_api_token="t" * 30,
        tz=ZoneInfo("Asia/Bishkek"),
        developer_tariff=True,
        db_path=tmp_path / "bot.db",
    )
    session = FakeSession()
    bot = Bot(cfg.tg_token, session=session)
    dp = Dispatcher()
    dp.include_router(build_router(Deps(db, api, engine, cfg, runtime)))

    async def send(update):
        session.calls.clear()
        await dp.feed_update(bot, update)
        return session.texts()

    return send


async def test_stranger_is_ignored(panel):
    assert await panel(text_update(STRANGER, "/start")) == []


async def test_start_shows_help(panel):
    texts = await panel(text_update(OWNER, "/start"))
    assert "Пульт рассылки" in texts[0]


async def test_order_flow_one_and_all(panel, db):
    texts = await panel(text_update(OWNER, BTN_ORDERS))
    assert "№1: — пусто —" in texts[0]
    await panel(callback_update(OWNER, "o:set:2"))
    texts = await panel(text_update(OWNER, "Ташкент - Ош\nТент 120, 800$"))
    assert "Заказ №2 сохранён" in texts[0]
    assert [o["slot"] for o in await db.active_orders()] == [2]

    await panel(callback_update(OWNER, "o:all"))
    texts = await panel(text_update(OWNER, "Заказ A\n\nЗаказ B\n\nЗаказ C"))
    assert "Сохранено заказов: 3" in texts[0]
    assert [o["text"] for o in await db.active_orders()] == ["Заказ A", "Заказ B", "Заказ C"]

    await panel(callback_update(OWNER, "o:all"))
    texts = await panel(text_update(OWNER, "1\n\n2\n\n3\n\n4"))
    assert "слотов 3" in texts[0]

    texts = await panel(callback_update(OWNER, "o:del:3"))
    assert any("Заказ №3 снят" in t for t in texts)


async def test_too_long_summary_rejected(panel, db):
    await panel(callback_update(OWNER, "o:set:1"))
    texts = await panel(text_update(OWNER, "x" * 1300))
    assert "максимум 1200" in texts[0]
    assert await db.active_orders() == []


async def test_groups_sync_toggle_and_developer_limit(panel, api, db):
    api.groups = [{"id": f"12036300{i}@g.us", "name": f"Группа {i}", "type": "group"} for i in range(5)]
    texts = await panel(text_update(OWNER, BTN_GROUPS))
    assert "Список пуст" in texts[0]
    texts = await panel(callback_update(OWNER, "g:sync:0"))
    assert any("Групп: 5, новых 5" in t for t in texts)
    for i in range(3):
        await panel(callback_update(OWNER, f"g:t:0:12036300{i}@g.us"))
    texts = await panel(callback_update(OWNER, "g:t:0:120363003@g.us"))
    assert any("только 3 группы" in t for t in texts)
    assert await db.enabled_count() == 3
    texts = await panel(callback_update(OWNER, "g:k:0:120363000@g.us"))
    assert any("тихая" in t for t in texts)
    assert (await db.get_group("120363000@g.us"))["kind"] == "quiet"


async def test_start_requires_groups_and_orders(panel, db, api):
    texts = await panel(text_update(OWNER, BTN_START))
    assert "Включите хотя бы одну группу" in texts[0]
    await db.sync_groups([("1@g.us", "Ош логистика")])
    await db.set_group_enabled("1@g.us", True)
    texts = await panel(text_update(OWNER, BTN_START))
    assert "Добавьте заказы" in texts[0]
    await db.set_order(1, "Заказ", OWNER)
    texts = await panel(text_update(OWNER, BTN_START))
    assert "Рассылка запущена: 1 групп" in texts[0]
    assert await db.get("running") == "1"
    texts = await panel(text_update(OWNER, BTN_STATUS))
    assert "Рассылка: идёт" in texts[0] and "Номер: ✅ в сети" in texts[0]


async def test_start_refused_when_number_offline(panel, runtime):
    runtime.wa_state = "notAuthorized"
    texts = await panel(text_update(OWNER, BTN_START))
    assert "не подключён" in texts[0]


async def test_settings_owner_only_and_validation(panel, db):
    texts = await panel(text_update(OPERATOR, "/set live 20"))
    assert "только владелец" in texts[0]
    texts = await panel(text_update(OWNER, "/set live 20"))
    assert "раз в 20 мин" in texts[0]
    assert await db.get("live_interval") == "20"
    texts = await panel(text_update(OWNER, "/set pause 5 10"))
    assert "от 10 до 300" in texts[0]
    texts = await panel(text_update(OWNER, "/set night 22:30 06:00"))
    assert "22:30–06:00" in texts[0]
    texts = await panel(text_update(OWNER, "/set footer 📞 +996 555 000 000"))
    assert "+996" in texts[0]
    texts = await panel(text_update(OWNER, BTN_SETTINGS))
    assert "Подвал: 📞 +996 555 000 000" in texts[0]


async def test_test_send(panel, db, api, engine):
    await db.sync_groups([("1@g.us", "Ош логистика")])
    await db.set_group_enabled("1@g.us", True)
    await db.set_order(1, "Заказ", OWNER)
    texts = await panel(text_update(OWNER, BTN_TEST))
    assert "Отправить сводку сейчас в 1 групп" in texts[0]
    await panel(callback_update(OWNER, "t:go"))
    for _ in range(20):
        if api.sent:
            break
        await asyncio.sleep(0.05)
    assert api.sent and api.sent[0][0] == "1@g.us"
    assert await db.get("running") == "0"  # тест не запускает расписание


async def test_fallback_for_unknown_text(panel):
    texts = await panel(text_update(OWNER, "привет"))
    assert "Не понял" in texts[0]


@pytest.mark.parametrize(
    "button", [BTN_STOP_ALL, BTN_PAUSE, BTN_STATUS, BTN_GROUPS, BTN_START, BTN_SETTINGS, BTN_TEST]
)
async def test_menu_button_during_order_input_is_not_saved_as_order(panel, db, button):
    """Кнопка меню посреди ввода заказа срабатывает как кнопка. Иначе её подпись станет текстом заказа и уйдёт в WhatsApp."""
    await db.set_order(1, "Заказ", OWNER)
    await panel(callback_update(OWNER, "o:set:2"))  # бот ждёт текст заказа №2
    await panel(text_update(OWNER, button))
    assert [o["slot"] for o in await db.active_orders()] == [1]


async def test_stop_all_works_during_order_input(panel, db, engine):
    await db.sync_groups([("1@g.us", "Ош логистика")])
    await db.set_group_enabled("1@g.us", True)
    await db.set_order(1, "Заказ", OWNER)
    await engine.start()
    await panel(callback_update(OWNER, "o:set:2"))
    texts = await panel(text_update(OWNER, BTN_STOP_ALL))
    assert any("Всё остановлено" in t for t in texts)
    assert await db.get("running") == "0"


async def test_set_command_during_order_input_is_not_saved_as_order(panel, db):
    await panel(callback_update(OWNER, "o:set:2"))
    texts = await panel(text_update(OWNER, "/set live 20"))
    assert "раз в 20 мин" in texts[0]
    assert await db.active_orders() == []
