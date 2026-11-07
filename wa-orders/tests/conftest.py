from __future__ import annotations

import random
from zoneinfo import ZoneInfo

import pytest

from app.db import Database
from app.engine import Engine, Runtime
from app.greenapi import GreenAPIError

TZ = ZoneInfo("Asia/Bishkek")


class FakeAPI:
    """Поддельный GREEN-API: запоминает отправки, умеет падать по сценарию."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail_next: list[GreenAPIError] = []
        self.outgoing: list[dict] = []
        self.cleared = 0
        self.queue: list = []
        self.groups: list[dict] = []
        self.settings: dict = {}
        self.set_calls: list[dict] = []
        self.state = "authorized"
        self.rebooted = 0

    async def send_message(self, chat_id, text):
        if self.fail_next:
            error = self.fail_next.pop(0)
            if getattr(error, "went_out", False):  # таймаут, но сообщение на самом деле ушло
                self.outgoing.append({"chatId": chat_id, "textMessage": text, "idMessage": "REAL1"})
            raise error
        self.sent.append((chat_id, text))
        return f"MSG{len(self.sent)}"

    async def last_outgoing(self, minutes=10):
        return list(self.outgoing)

    async def clear_queue(self):
        self.cleared += 1
        self.queue.clear()

    async def show_queue(self):
        return list(self.queue)

    async def get_groups(self):
        return list(self.groups)

    async def get_settings(self):
        return dict(self.settings)

    async def set_settings(self, settings):
        self.set_calls.append(settings)
        self.settings.update(settings)

    async def get_state(self):
        return self.state

    async def reboot(self):
        self.rebooted += 1


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    async def owner(self, text):
        self.messages.append(text)

    async def admins(self, text):
        self.messages.append(text)


class Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


# 07.11.2026 12:00 по Бишкеку — дневное время, не тихие часы
NOON = 1794031200.0


@pytest.fixture
async def db(tmp_path, clock):
    database = await Database.open(tmp_path / "bot.db", clock=clock)
    yield database
    await database.close()


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def clock():
    return Clock(NOON)


@pytest.fixture
def runtime():
    return Runtime(wa_state="authorized")


@pytest.fixture
def engine(db, api, notifier, runtime, clock):
    return Engine(db, api, notifier, runtime, TZ, clock=clock, rng=random.Random(7), verify_delay=0)


async def add_groups(db, count: int, enabled: bool = True):
    await db.sync_groups([(f"1203630000{i}@g.us", f"Группа {i}") for i in range(count)])
    if enabled:
        for i in range(count):
            await db.set_group_enabled(f"1203630000{i}@g.us", True)
