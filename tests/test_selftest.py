"""Самопроверка (--selftest): Telegram, мост и MT5 по отдельности, расшифровка ошибок, голос по флагу."""
import asyncio
import sqlite3
import sys

import pytest

from agent import selftest
from agent.bridge.hub import BridgeHub
from agent.selftest import TelegramCheck
from agent.tg.diagnose import explain_tg_error
from tests.conftest import KEY
from tests.test_brain import mk_settings
from tools.fake_mt5 import FakeMT5Source, synthetic_m5

OWNER = 777


def err(name: str, text: str = "", **attrs) -> Exception:
    """Ошибка с нужным ИМЕНЕМ класса, как у pyrogram.errors (сам pyrogram в CI не ставится)."""
    e = type(name, (Exception,), {})(text)
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


# ---------------------------------------------------------------- расшифровка ошибок Telegram
@pytest.mark.parametrize("name,needle", [
    ("AuthKeyUnregistered", "--login"), ("SessionRevoked", "--login"), ("UserDeactivated", "--login"),
    ("AuthKeyDuplicated", "только в одном месте"), ("UserDeactivatedBan", "заблокирован"),
    ("PhoneNumberBanned", "заблокирован"), ("ApiIdInvalid", "my.telegram.org"),
    ("PeerIdInvalid", "напиши ему любое сообщение"), ("UserIsBlocked", "заблокировал"),
])
def test_explain_known_telegram_errors(name, needle):
    assert needle in explain_tg_error(err(name))


def test_explain_floodwait_names_seconds_and_network_and_locked_session():
    assert "30 с" in explain_tg_error(err("FloodWait", "x", value=30))
    assert "не отвечает" in explain_tg_error(asyncio.TimeoutError())
    assert "нет соединения" in explain_tg_error(ConnectionResetError("boom"))
    assert "уже запущен" in explain_tg_error(sqlite3.OperationalError("database is locked"))
    assert "повреждён" in explain_tg_error(sqlite3.DatabaseError("file is not a database"))
    assert "нет соединения с Telegram: ответ не тот" in explain_tg_error(ConnectionError("ответ не тот"))
    assert explain_tg_error(RuntimeError("странное")) == "RuntimeError: странное"


def test_explain_matches_real_pyrogram_errors_when_installed():
    errors = pytest.importorskip("pyrogram.errors")
    assert "--login" in explain_tg_error(errors.AuthKeyUnregistered())
    assert "напиши ему любое сообщение" in explain_tg_error(errors.PeerIdInvalid())
    assert "заблокирован" in explain_tg_error(errors.UserDeactivatedBan())
    assert "с (FloodWait)" in explain_tg_error(errors.FloodWait(value=45))


# ---------------------------------------------------------------- три проверки Telegram
class Probe:
    def __init__(self, connect=True, me=None, owner=None, send=None, connect_exc=None, hang=False):
        self._connect, self._connect_exc, self._hang = connect, connect_exc, hang
        self._me = me or {"id": 555, "name": "Консильери", "username": "consig_bot", "phone": ""}
        self._owner, self._send = owner, send
        self.sent: list[tuple[int, str]] = []
        self.disconnected = 0

    async def connect(self):
        if self._hang:
            await asyncio.sleep(10)
        if self._connect_exc:
            raise self._connect_exc
        return self._connect

    async def me(self):
        return self._me

    async def owner(self, owner_id):
        if isinstance(self._owner, Exception):
            raise self._owner
        return self._owner or {"id": owner_id, "name": "Абу"}

    async def send(self, owner_id, text):
        if isinstance(self._send, Exception):
            raise self._send
        self.sent.append((owner_id, text))
        return 42

    async def disconnect(self):
        self.disconnected += 1


@pytest.fixture
def tgs(tmp_path):
    """Настройки с api_id и файлом сессии."""
    s = mk_settings(owner_id=OWNER, tg_api_id=12345, tg_api_hash="abc", data_dir=str(tmp_path))
    (tmp_path / "consigliere.session").write_bytes(b"x")
    return s


async def run3(chk: TelegramCheck):
    try:
        return [await chk.login(), await chk.owner(), await chk.send()]
    finally:
        await chk.close()


async def test_telegram_all_three_pass_and_test_message_is_sent(tgs):
    probe = Probe()
    r = await run3(TelegramCheck(tgs, lambda: probe))
    assert [x[0] for x in r] == [True, True, True]
    assert "Консильери" in r[0][1] and "Абу" in r[1][1] and "id 42" in r[2][1]
    assert probe.sent == [(OWNER, "✅ Самопроверка Консильери: Telegram работает.")] and probe.disconnected == 1


async def test_telegram_no_send_flag_skips_message(tgs):
    probe = Probe()
    r = await run3(TelegramCheck(tgs, lambda: probe, send_test=False))
    assert r[2][0] is None and probe.sent == []


async def test_telegram_missing_api_id_and_missing_session_never_prompt(tmp_path):
    s = mk_settings(owner_id=OWNER, tg_api_id=0, tg_api_hash="", data_dir=str(tmp_path))
    called = []
    chk = TelegramCheck(s, lambda: called.append(1))
    ok, msg = await chk.login()
    assert ok is False and "TG_API_ID" in msg and not called
    s2 = mk_settings(owner_id=OWNER, tg_api_id=1, tg_api_hash="h", data_dir=str(tmp_path))     # сессии нет
    ok, msg = await TelegramCheck(s2, lambda: called.append(1)).login()
    assert ok is False and "--login" in msg and not called                                   # даже не создаём клиента


async def test_telegram_failed_login_skips_the_rest_and_counts_one_problem(tgs, capsys):
    probe = Probe(connect_exc=sqlite3.OperationalError("database is locked"))
    chk = TelegramCheck(tgs, lambda: probe)
    code = await selftest.run_selftest([("Telegram: вход", chk.login), ("Telegram: владелец", chk.owner),
                                        ("Telegram: отправка", chk.send)])
    out = capsys.readouterr().out
    assert code == 1 and "✖ Telegram: вход" in out and "уже запущен" in out
    assert "– Telegram: владелец: пропущено" in out and "– Telegram: отправка: пропущено" in out
    assert "проблем: 1" in out and probe.disconnected == 1


async def test_telegram_unauthorized_session_file(tgs):
    ok, msg = await TelegramCheck(tgs, lambda: Probe(connect=False)).login()
    assert ok is False and "вход не завершён" in msg and "--login" in msg


async def test_telegram_session_of_the_owner_himself_is_rejected(tgs):
    probe = Probe(me={"id": OWNER, "name": "Абу", "username": "abu", "phone": ""})
    ok, msg = await TelegramCheck(tgs, lambda: probe).login()
    assert ok is False and "отдельный аккаунт" in msg


async def test_telegram_owner_unknown_and_blocked_and_flood(tgs):
    r = await run3(TelegramCheck(tgs, lambda: Probe(owner=err("PeerIdInvalid"))))
    assert r[1][0] is False and "напиши ему любое сообщение" in r[1][1]
    r = await run3(TelegramCheck(tgs, lambda: Probe(send=err("UserIsBlocked"))))
    assert r[2][0] is False and "заблокировал" in r[2][1]
    r = await run3(TelegramCheck(tgs, lambda: Probe(send=err("FloodWait", value=33))))
    assert r[2][0] is False and "33 с" in r[2][1]


async def test_telegram_no_owner_id_is_named(tmp_path):
    s = mk_settings(owner_id=0, tg_api_id=1, tg_api_hash="h", data_dir=str(tmp_path))
    (tmp_path / "consigliere.session").write_bytes(b"x")
    r = await run3(TelegramCheck(s, lambda: Probe()))
    assert r[1][0] is False and "OWNER_ID" in r[1][1] and r[2][0] is None


async def test_telegram_hanging_connection_times_out_with_a_reason(tgs):
    probe = Probe(hang=True)
    ok, msg = await TelegramCheck(tgs, lambda: probe, timeout=0.05).login()
    assert ok is False and "не отвечает" in msg and probe.disconnected == 1


# ---------------------------------------------------------------- MT5 отдельно
class State(FakeMT5Source):
    def __init__(self, state=None, **kw):
        super().__init__(synthetic_m5(weeks=3), **kw)
        self.state = {"connected": True, "trade_allowed": True, "login": 5043210, "server": "Broker-Demo", "demo": True}
        self.state.update(state or {})
        self.no_symbol = False

    def terminal_state(self):
        return self.state

    def find_symbol(self, preferred=""):
        return None if self.no_symbol else super().find_symbol(preferred)


def test_mt5_ok_shows_demo_account():
    ok, msg = selftest.check_mt5(State())
    assert ok is True and "ДЕМО" in msg and "5043210" in msg and "Broker-Demo" in msg


def test_mt5_real_account_is_flagged():
    ok, msg = selftest.check_mt5(State({"demo": False}))
    assert ok is True and "РЕАЛЬНЫЙ" in msg


def test_mt5_terminal_path_missing(tmp_path):
    ok, msg = selftest.check_mt5(State(), path=str(tmp_path / "terminal64.exe"))
    assert ok is False and "MT5_PATH" in msg
    f = tmp_path / "terminal64.exe"
    f.write_bytes(b"x")
    assert selftest.check_mt5(State(), path=str(f))[0] is True


def test_mt5_not_running_and_package_missing():
    from agent.analysis.data_mt5 import MT5Unavailable
    src = State()
    src.fail = True
    ok, msg = selftest.check_mt5(src)
    assert ok is False and "не запущен" in msg and "демо-счёт" in msg
    src2 = State()
    src2.find_symbol = lambda p="": (_ for _ in ()).throw(MT5Unavailable("пакет MetaTrader5 недоступен (нужен Windows)"))
    ok, msg = selftest.check_mt5(src2)
    assert ok is False and "install.bat" in msg


def test_mt5_no_broker_link_algo_off_and_no_symbol():
    ok, msg = selftest.check_mt5(State({"connected": False}))
    assert ok is False and "нет связи с брокером" in msg
    ok, msg = selftest.check_mt5(State({"trade_allowed": False}))
    assert ok is False and "Алготорговля" in msg
    src = State()
    src.no_symbol = True
    ok, msg = selftest.check_mt5(src)
    assert ok is False and "символ золота" in msg


# ---------------------------------------------------------------- мост отдельно
async def test_bridge_ping_timeout_names_the_cause(paths, db, ea):
    ea.write_status()                                  # status.json свежий, но цикл робота не запущен: команды без ответа
    ok, msg = await selftest.check_bridge(BridgeHub(paths, db, KEY), paths, KEY, ping_timeout=0.3)
    assert ok is False and "не отвечает на команды" in msg and "0.3 с" in msg


async def test_bridge_wrong_key_is_named(paths, db, ea):
    from tests.fakes import robot_loop
    task = asyncio.create_task(robot_loop(ea))
    ok, msg = await selftest.check_bridge(BridgeHub(paths, db, "чужой-ключ"), paths, "чужой-ключ", ping_timeout=3)
    task.cancel()
    assert ok is False and "InpBridgeKey" in msg


async def test_bridge_folder_missing():
    ok, msg = await selftest.check_bridge(None, None, KEY)
    assert ok is False and "папка моста" in msg


# ---------------------------------------------------------------- пропуск и голос
async def test_skipped_checks_do_not_fail_the_selftest(capsys):
    code = await selftest.run_selftest([("А", lambda: (True, "хорошо")), ("Б", lambda: (None, "пропущено: нечего"))])
    out = capsys.readouterr().out
    assert code == 0 and "✔ А" in out and "– Б: пропущено" in out and "всё в порядке" in out


def test_voice_flag_text_and_defaults():
    off = mk_settings()
    on = mk_settings(voice_enabled=True)
    assert off.voice_enabled is False and off.stt_model == "base"
    assert "голос выключен" in selftest.check_voice_flag(off)[1]
    assert "голос включён" in selftest.check_voice_flag(on)[1] and "base" in selftest.check_voice_flag(on)[1]


def test_voice_off_builds_nothing_and_imports_no_whisper():
    from agent.main import build_stt_tts
    assert build_stt_tts(mk_settings()) == (None, None)
    assert "faster_whisper" not in sys.modules and "torch" not in sys.modules
    stt, tts = build_stt_tts(mk_settings(voice_enabled=True))
    assert stt.model_size == "base" and tts.voice == "ru-RU-DmitryNeural"       # создаются, но модель не грузится


def test_calls_library_check_matches_py_tgcalls_3():
    pytest.importorskip("pytgcalls")
    ok, msg = selftest.check_calls_lib()
    assert ok is True and "py-tgcalls" in msg


async def test_real_pyrogram_probe_on_a_corrupt_session_file_gives_a_clear_reason(tmp_path):
    """Настоящий Client, битый файл сессии: падает до сети, причина названа по-русски (проверено живьём)."""
    pytest.importorskip("pyrogram")
    from agent.tg.client import PyrogramProbe
    s = mk_settings(owner_id=OWNER, tg_api_id=12345, tg_api_hash="0123456789abcdef0123456789abcdef",
                    data_dir=str(tmp_path))
    (tmp_path / "consigliere.session").write_bytes(b"not a sqlite file")
    chk = TelegramCheck(s, lambda: PyrogramProbe(s.tg_api_id, s.tg_api_hash, s.tg_phone, s.data_dir), timeout=15)
    try:
        ok, msg = await chk.login()
    finally:
        await chk.close()
    assert ok is False and "повреждён" in msg and "--login" in msg
