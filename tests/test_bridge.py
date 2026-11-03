"""Мост: события, курсор, команды, диапазоны, статус."""
import asyncio
import json

import pytest

from agent.bridge.events import EventReader
from agent.bridge.hub import BridgeHub
from agent.bridge.protocol import read_status, validate_command
from agent.bridge.trades import compute, read_trades, format_table
from tests.conftest import KEY


# ---------- события и курсор ----------
async def test_first_start_reads_from_end(paths, db, ea):
    ea.emit("zone_confirmed", 1)
    ea.emit("bias_changed", 1)
    reader = EventReader(paths, db)
    assert await reader.poll() == []  # история не повторяется
    ea.emit("zone_touch", 1)
    got = await reader.poll()
    assert [e.type for e in got] == ["zone_touch"]


async def test_restart_does_not_duplicate(paths, db, ea):
    r1 = EventReader(paths, db)
    await r1.poll()
    ea.emit("zone_touch", 1)
    ea.emit("zone_confirmed", 1)
    assert len(await r1.poll()) == 2
    r2 = EventReader(paths, db)  # новый процесс, курсор из базы
    assert await r2.poll() == []
    ea.emit("bias_changed", 1)
    assert [e.type for e in await r2.poll()] == ["bias_changed"]


async def test_partial_line_waits(paths, db, ea):
    reader = EventReader(paths, db)
    await reader.poll()
    f = next(paths.events_dir.glob("*.jsonl"))
    line = json.dumps({"seq": ea.seq + 1, "type": "zone_touch", "level": 1, "data": {}}, ensure_ascii=False)
    with open(f, "ab") as fh:
        fh.write(line[:20].encode())  # без \n
    assert await reader.poll() == []
    with open(f, "ab") as fh:
        fh.write((line[20:] + "\n").encode())
    got = await reader.poll()
    assert [e.type for e in got] == ["zone_touch"]


async def test_seq_gap_is_logged(paths, db, ea):
    reader = EventReader(paths, db)
    await reader.poll()
    ea.emit("zone_touch", 1)
    await reader.poll()
    ea.seq += 5  # робот «потерял» события
    ea.emit("bias_changed", 1)
    await reader.poll()
    assert reader.gaps and reader.gaps[0][0] == ea.seq - 5


async def test_bot_started_before_robot_reads_first_events(paths, db):
    # папки есть, а робот ещё ничего не писал
    reader = EventReader(paths, db)
    assert await reader.poll() == []
    from tools.fake_ea import FakeEA
    e = FakeEA(paths, KEY)
    e.start()
    got = await reader.poll()
    assert [x.type for x in got] == ["ea_started"]


async def test_russian_text_utf8(paths, db, ea):
    reader = EventReader(paths, db)
    await reader.poll()
    ea.emit("alert", 0, "critical", {"key": "disconnect", "state": "open"}, "🚨 ТРЕВОГА: нет связи с брокером")
    got = await reader.poll()
    assert got[0].text == "🚨 ТРЕВОГА: нет связи с брокером"


# ---------- команды ----------
async def test_command_roundtrip(paths, db, ea):
    hub = BridgeHub(paths, db, KEY)
    await hub.poll_once()  # курсор в конец

    async def robot():
        for _ in range(50):
            await asyncio.sleep(0.05)
            if ea.process_commands():
                return

    t = asyncio.create_task(robot())
    res = await hub.command("set", {"risk_pct": 0.5}, timeout=3)
    await t
    assert res.ok and "risk_pct=0.5" in res.msg
    assert ea.set["risk_pct"] == 0.5
    saved = json.loads(paths.settings.read_text(encoding="utf-8"))
    assert saved["settings"]["risk_pct"] == 0.5  # переживает перезапуск


async def test_command_timeout_when_robot_silent(paths, db, ea):
    hub = BridgeHub(paths, db, KEY)
    res = await hub.command("ping", timeout=0.5)
    assert not res.ok and res.timeout


async def test_expired_command_rejected(paths, db, ea):
    hub = BridgeHub(paths, db, KEY)
    cid = hub.write_command("ping")
    f = paths.cmd_dir / f"{cid}.json"
    body = json.loads(f.read_text())
    body["expires_utc"] = "2020-01-01T00:00:00Z"
    f.write_text(json.dumps(body))
    ea.process_commands()
    last = [json.loads(l) for l in next(paths.events_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()][-1]
    assert last["type"] == "cmd_result" and last["data"]["ok"] is False and "устарела" in last["data"]["msg"]
    assert not list(paths.cmd_dir.glob("*.json"))  # робот удаляет файл


async def test_wrong_key_rejected(paths, db, ea):
    hub = BridgeHub(paths, db, "чужой-ключ")
    hub.write_command("pause")
    ea.process_commands()
    assert ea.set["paused"] is False


async def test_empty_robot_key_rejects_all(paths):
    from tools.fake_ea import FakeEA
    e = FakeEA(paths, "")
    e.start()
    hub = BridgeHub(paths, None, "")  # type: ignore[arg-type]
    hub.write_command("pause")
    e.process_commands()
    assert e.set["paused"] is False


def test_command_written_atomically(paths, db):
    hub = BridgeHub(paths, db, KEY)
    hub.write_command("ping")
    assert not list(paths.cmd_dir.glob("*.tmp"))
    assert len(list(paths.cmd_dir.glob("*.json"))) == 1


# ---------- диапазоны ----------
@pytest.mark.parametrize("cmd,args,ok", [
    ("set", {"risk_pct": 0.5}, True), ("set", {"risk_pct": 0.05}, False), ("set", {"risk_pct": 5.1}, False),
    ("set", {"daily_risk_pct": 20}, True), ("set", {"daily_risk_pct": 21}, False),
    ("set", {"max_trades": 10}, True), ("set", {"max_trades": 11}, False), ("set", {"max_trades": 2.5}, False),
    ("set", {"rr": 1}, True), ("set", {"rr": 0.9}, False), ("set", {"weak": False}, True), ("set", {"weak": "no"}, False),
    ("set", {}, False), ("set", {"что-то": 1}, False),
    ("open", {"side": "BUY", "sl_pts": 5.5}, True), ("open", {"side": "BUY"}, False),
    ("open", {"side": "LONG", "sl_pts": 5}, False), ("open", {"side": "SELL", "sl_pts": 0}, False),
    ("open", {"side": "SELL", "sl_pts": 5, "risk_pct": 9}, False),
    ("modify", {"sl": 2640.0}, True), ("modify", {}, False), ("modify", {"sl": -1}, False),
    ("chart", {"tf": "H4"}, True), ("chart", {"tf": "D1"}, False),
    ("pause", {}, True), ("взорви", {}, False),
])
def test_validate_command(cmd, args, ok):
    assert validate_command(cmd, args)[0] is ok


# ---------- сценарии робота ----------
async def test_close_without_position(paths, db, ea):
    hub = BridgeHub(paths, db, KEY)
    hub.write_command("close")
    ea.process_commands()
    last = json.loads(next(paths.events_dir.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[-1])
    assert last["data"]["ok"] is False and "нет" in last["data"]["msg"]


async def test_manual_open_close_trade_csv(paths, db, ea):
    hub = BridgeHub(paths, db, KEY)
    hub.write_command("open", {"side": "BUY", "sl_pts": 5.5})
    ea.process_commands()
    assert ea.position and ea.position["manual"] and ea.position["trigger"] == "MANUAL"
    ea.set_price(ea.bid + 11.0)
    rec = ea.close_position("TP")
    assert rec["result_usd"] > 0
    trades = read_trades(paths.trades_csv)
    assert len(trades) == 1 and trades[0]["manual"] is True and trades[0]["exit"] == "TP"


async def test_status_json_and_bot_alive(paths, db, ea):
    hub = BridgeHub(paths, db, KEY)
    st = read_status(paths)
    assert st and st.get("schema") == 1 and st.age_sec < 5
    assert st.get("bridge", "bot_alive") is False
    hub.write_heartbeat()
    ea.write_status()
    assert read_status(paths).get("bridge", "bot_alive") is True


def test_stats_winrate_excludes_be_and_marks_few(paths):
    trades = [{"result_usd": 20, "result_pts": 10, "rr_real": 2, "exit": "TP"},
              {"result_usd": -10, "result_pts": -5, "rr_real": -1, "exit": "SL"},
              {"result_usd": 0, "result_pts": 0, "rr_real": 0, "exit": "BE"}]
    s = compute(trades)
    assert s.n == 3 and s.be == 1 and s.winrate == 50.0 and s.profit_factor == 2.0
    assert s.few
    assert "мало данных" in format_table(trades)


async def test_cleanup_old_files(paths, db, ea):
    import os, time
    old = paths.shots_dir / "1.png"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    fresh = paths.shots_dir / "2.png"
    fresh.write_bytes(b"x")
    hub = BridgeHub(paths, db, KEY)
    r = hub.cleanup()
    assert r["shots"] == 1 and not old.exists() and fresh.exists()
