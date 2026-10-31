"""Сборка целиком на двойниках: событие робота -> Telegram, сообщение -> ответ, «да», звонок, самопроверка, консоль."""
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agent import selftest
from agent.bridge.hub import BridgeHub
from agent.bridge.protocol import BridgePaths
from agent.brain.llm import LLM
from agent.runtime import Runtime, find_robot
from agent.tg.client import Messenger, SendQueue
from tests.conftest import KEY
from tests.fakes import FakeClaude, robot_loop, text_resp, tool_resp
from tests.test_brain import mk_settings
from tests.test_calls_tg import FakeAssistant, FakeBackend, FakeSTT, FakeTTS, FakeTransport
from tools.fake_mt5 import FakeMT5Source, synthetic_m5

OWNER = 777


class CallBackendFake(FakeBackend):
    def __init__(self):
        super().__init__()
        self.cbs = {}

    def on_audio(self, cb): self.cbs["audio"] = cb
    def on_ended(self, cb): self.cbs["ended"] = cb
    def on_incoming(self, cb): self.cbs["incoming"] = cb

    async def accept(self, user_id):
        self.accepted = user_id


class TTSFake(FakeTTS):
    async def ogg_opus(self, text):
        return b"OggS-fake"


@pytest.fixture
async def rt(paths, db, ea, tmp_path):
    s = mk_settings(owner_id=OWNER, bridge_root=str(paths.root), data_dir=str(tmp_path), call_max_min=1)
    tr = FakeTransport()
    claude = FakeClaude()
    backend = CallBackendFake()
    r = Runtime(s, db, Messenger(tr, SendQueue(min_interval=0)), FakeMT5Source(synthetic_m5(weeks=110, seed=11)),
                TTSFake(), FakeSTT("привет"), backend=backend, llm=LLM(s, db, client=claude), system_static="СИСТЕМА")
    await r.attach_robot(paths)
    r.hub.inner.stop  # noqa: B018
    task = asyncio.create_task(robot_loop(ea))
    yield r, tr, claude, ea, backend
    task.cancel()
    await r.stop()


async def pump(r, n=3):
    for _ in range(n):
        await r.hub.inner.poll_once()
        await asyncio.sleep(0.02)


async def test_robot_event_reaches_owner_with_template(rt):
    r, tr, claude, ea, _ = rt
    await r.hub.inner.poll_once()
    ea.set_price(2650.0)
    ea.open_position("BUY", 5.5, entry=2650.0)
    ea.set_price(2661.0)
    ea.close_position("TP", 2661.0)
    await pump(r)
    texts = [t[1] for t in tr.sent if t[0] == "text"]
    assert any("TP" in t and "+11.0 pt" in t for t in texts)
    opened = next(t for t in texts if "Вход:" in t)
    assert "SL:" in opened and "TP:" in opened and "риск" in opened


async def test_end_to_end_risk_change_needs_yes(rt):
    r, tr, claude, ea, _ = rt
    claude.script = [tool_resp("set_risk", {"pct": 0.5})]
    outs = await r.on_message(OWNER, "abu", text="поставь риск 0.5")
    assert outs[-1].text.endswith("Да / нет") and ea.set["risk_pct"] == 1.0
    outs = await r.on_message(OWNER, "abu", text="да")
    assert "Готово" in outs[0].text and ea.set["risk_pct"] == 0.5
    assert tr.sent[-1][1].startswith("✔️ Готово")


async def test_strangers_get_silence_and_owner_gets_one_notice(rt):
    r, tr, claude, ea, _ = rt
    assert await r.on_message(999, "spammer", text="дай зону") == []
    assert await r.on_message(998, None, text="закрой всё") == []
    notices = [t for t in tr.sent if "незнакомый" in str(t[1])]
    assert len(notices) == 1 and claude.calls == []


async def test_voice_message_answers_with_voice_plus_text(rt, tmp_path):
    r, tr, claude, ea, _ = rt
    claude.script = [text_resp("Слушаю, брат.")]
    f = tmp_path / "v.ogg"
    f.write_bytes(b"x")
    outs = await r.on_message(OWNER, "abu", voice_path=str(f))
    assert outs[-1].text == "Слушаю, брат." and outs[-1].voice is True
    kinds = [t[0] for t in tr.sent]
    assert "text" in kinds and "voice" in kinds


async def test_unrecognised_voice_asks_to_repeat(rt, tmp_path):
    r, tr, claude, ea, _ = rt
    r.stt = FakeSTT("")
    f = tmp_path / "v.ogg"
    f.write_bytes(b"x")
    outs = await r.on_message(OWNER, "abu", voice_path=str(f))
    assert "повтори" in outs[0].text and claude.calls == []


async def test_tradingview_hint_once(rt):
    r, tr, claude, ea, _ = rt
    claude.script = [text_resp("ок"), text_resp("ок")]
    await r.on_message(OWNER, "abu", text="а на TradingView зона другая")
    await r.on_message(OWNER, "abu", text="на tradingview опять другая")
    assert len([t for t in tr.sent if "TradingView" in str(t[1]) and "фиде" in str(t[1])]) == 1


async def test_incoming_call_from_stranger_declined_from_owner_served(rt):
    r, tr, claude, ea, backend = rt
    assert await backend.cbs["incoming"](555) == "declined" and backend.declined == [555]
    r.assistant = NS()                                                        # звонок не должен звать модель без речи
    task = asyncio.create_task(backend.cbs["incoming"](OWNER))
    await asyncio.sleep(0.4)
    backend.cbs["ended"]()                                                    # Абу положил трубку
    assert await asyncio.wait_for(task, 3) == "ended"
    assert backend.accepted == OWNER and r.sessions_log[-1]["reason"] == "ended"


def test_find_robot_single_and_freshest(tmp_path):
    s = mk_settings(bridge_root=str(tmp_path))
    assert find_robot(s) is None
    a = BridgePaths(tmp_path, "111_1"); a.ensure(); a.status.write_text("{}")
    assert find_robot(s).robot == "111_1"
    b = BridgePaths(tmp_path, "222_2"); b.ensure(); b.status.write_text("{}")
    os.utime(a.status, (1, 1))
    assert find_robot(s).robot == "222_2"
    s2 = mk_settings(bridge_root=str(tmp_path), bridge_robot="111_1")
    assert find_robot(s2).robot == "111_1"


# ---------------------------------------------------------------- самопроверка
async def test_selftest_reports_in_russian_and_exit_code(paths, db, ea, capsys):
    hub = BridgeHub(paths, db, KEY)
    task = asyncio.create_task(robot_loop(ea))
    checks = [("MT5", lambda: selftest.check_mt5(FakeMT5Source(synthetic_m5(weeks=3)))),
              ("Мост", lambda: selftest.check_bridge(hub, paths, KEY)),
              ("Диск", lambda: selftest.check_disk(mk_settings(data_dir=str(paths.root))))]
    code = await selftest.run_selftest(checks)
    task.cancel()
    out = capsys.readouterr().out
    assert code == 0 and "✔ MT5" in out and "✔ Мост" in out and "ping прошёл" in out and "всё в порядке" in out


async def test_selftest_names_the_problem(paths, db, capsys):
    hub = BridgeHub(paths, db, KEY)
    src = FakeMT5Source(synthetic_m5(weeks=3))
    src.fail = True
    code = await selftest.run_selftest([("MT5", lambda: selftest.check_mt5(src)),
                                        ("Мост", lambda: selftest.check_bridge(hub, paths, KEY)),
                                        ("Мост без ключа", lambda: selftest.check_bridge(hub, paths, ""))])
    out = capsys.readouterr().out
    assert code == 1 and "✖ MT5" in out and "не запущен" in out and "status.json нет" in out and "Итог: проблем: 3" in out


# ---------------------------------------------------------------- консоль целиком (отдельный процесс)
def test_console_mode_smoke(tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path), "ANTHROPIC_API_KEY": "", "OWNER_ID": "1", "BRIDGE_KEY": "k",
           "PYTHONIOENCODING": "utf-8"}
    root = Path(__file__).resolve().parent.parent
    p = subprocess.run([sys.executable, "-m", "agent.main", "--console", "--fake-robot"], cwd=root, env=env,
                       input="как робот\nдай зону\nпоставь паузу\nда\n\n", text=True, capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr[-800:]
    assert "Робот работает" in p.stdout            # статус без ИИ из status.json симулятора
    assert "Золото " in p.stdout and "Решение за тобой" in p.stdout
    assert "Поставить паузу?" in p.stdout and "Готово" in p.stdout and "пауза включена" in p.stdout
