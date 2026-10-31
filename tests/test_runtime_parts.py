"""Уведомления, сторож, расписание, отчёты."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from agent.analysis.engine import AnalysisEngine
from agent.bridge.events import Event
from agent.bridge.hub import BridgeHub
from agent.brain.confirm import ConfirmManager, ExecResult
from agent.brain.llm import LLM
from agent.brain.tools import OutMsg, ToolContext, make_executor
from agent.notifier import Notifier, in_quiet_hours
from agent.reports import daily_report, evening_zones, morning_plan, weekly_report
from agent.scheduler import WEEKDAYS, Scheduler, parse_weekly
from agent.watchdog import Watchdog
from tests.conftest import KEY
from tests.fakes import FakeClaude, robot_loop, text_resp
from tests.test_brain import mk_settings
from tools.fake_mt5 import FakeMT5Source, synthetic_m5

BISHKEK = timezone(timedelta(hours=6))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeMessenger:
    def __init__(self):
        self.sent, self.texts, self.fail = [], [], False

    async def send(self, outs, reply_to=None, silent=False, tts=None):
        if self.fail:
            raise ConnectionError("нет интернета")
        self.sent.append({"outs": outs, "reply_to": reply_to, "silent": silent})
        return 100 + len(self.sent)

    async def text(self, text, reply_to=None, silent=False):
        if self.fail:
            raise ConnectionError("нет интернета")
        self.texts.append(text)


class FakeCaller:
    def __init__(self):
        self.calls = []

    async def call(self, key, spoken, text):
        self.calls.append((key, spoken, text))
        await asyncio.sleep(10)          # звонок «идёт», пока его не отменят
        return "answered"


def ev(type_, seq=1, level=0, severity="info", data=None, text=""):
    return Event(seq=seq, type=type_, level=level, severity=severity, data=data or {}, text=text, ts_local="2026-11-02 10:00:00")


@pytest.fixture
async def nw(paths, db, ea):
    s = mk_settings(call_on_critical=True)
    clock = Clock(datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc))      # 13:00 Бишкек
    hub = BridgeHub(paths, db, KEY)
    messenger = FakeMessenger()
    paused = []

    async def executor(kind, args):
        paused.append(kind)
        ea.set["paused"] = True
        ea.write_status()
        return ExecResult(True, "пауза включена")
    cm = ConfirmManager(db, executor, clock=lambda: clock.t.timestamp())
    caller = FakeCaller()

    async def nosleep(sec):
        return None
    n = Notifier(s, db, messenger, hub, cm, caller=caller, clock=clock, sleep=nosleep)
    return n, messenger, caller, ea, clock, paused, hub


# ---------------------------------------------------------------- уведомления
async def test_level_filter(nw):
    n, m, *_ = nw
    n.s.msg_level = 0
    await n.on_event(ev("zone_confirmed", level=1, text="🟩 Зона BUY"))
    assert m.sent == []
    n.s.msg_level = 1
    await n.on_event(ev("zone_confirmed", level=1, text="🟩 Зона BUY"))
    assert len(m.sent) == 1


async def test_trade_thread_replies_to_entry_message(nw, db):
    n, m, *_ = nw
    await n.on_event(ev("trade_opened", data={"ticket": 555, "side": "BUY", "entry": 2644.1, "sl": 2638.6, "tp": 2655.1,
                                              "lot": 0.02, "risk_usd": 11.0}, text="🟢 XAUUSD BUY · сделка 1/5"))
    assert m.sent[0]["reply_to"] is None and await db.get_trade_msg(555) == 101
    await n.on_event(ev("trade_closed", seq=2, data={"ticket": 555, "side": "BUY", "entry": 2644.1, "exit_price": 2655.1,
                                                     "pts": 11.0, "usd": 22.0, "exit": "TP", "duration_min": 85, "trigger": "A"}))
    assert m.sent[1]["reply_to"] == 101                                   # вся сделка одной веткой
    assert "TP" in m.sent[1]["outs"][0].text and "+22.00 USD" in m.sent[1]["outs"][0].text


def test_quiet_hours_range():
    rng = mk_settings().quiet_range()
    assert in_quiet_hours(datetime(2026, 11, 2, 3, 0), rng) and not in_quiet_hours(datetime(2026, 11, 2, 8, 0), rng)


async def test_quiet_hours_silence_all_but_alerts_and_trades(nw):
    n, m, caller, ea, clock, *_ = nw
    clock.t = datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)            # 03:00 Бишкек
    await n.on_event(ev("zone_touch", level=1, text="👀 Цена в зоне"))
    await n.on_event(ev("trade_opened", seq=2, data={"ticket": 1, "side": "BUY"}, text="🟢"))
    await n.on_event(ev("alert", seq=3, severity="warning", data={"key": "spread", "state": "open", "severity": "warning"}, text="спред"))
    assert [x["silent"] for x in m.sent] == [True, False, False]


async def test_critical_alert_calls_and_closed_alert_stops_the_call(nw):
    n, m, caller, ea, clock, *_ = nw
    ea.open_position("BUY", 5.5)
    data = {"key": "disconnect", "state": "open", "severity": "critical"}
    await n.on_event(ev("alert", severity="critical", data=data, text="С 14:32 (3 мин)."))
    await asyncio.sleep(0.05)
    assert "ТРЕВОГА" in m.sent[0]["outs"][0].text and m.sent[0]["silent"] is False
    assert len(caller.calls) == 1 and "Тревога" in caller.calls[0][1] and "Открыта BUY" in caller.calls[0][1]
    await n.on_event(ev("alert", seq=2, data={"key": "disconnect", "state": "closed", "severity": "critical"}, text="связь есть"))
    await asyncio.sleep(0.05)
    assert "Восстановлено" in m.sent[-1]["outs"][0].text and not n.is_open("disconnect")
    assert n._calls == {} or all(t.cancelled() or t.done() for t in n._calls.values())


async def test_warning_alert_is_text_only_and_duplicates_are_ignored(nw):
    n, m, caller, *_ = nw
    d = {"key": "spread", "state": "open", "severity": "warning"}
    await n.on_event(ev("alert", severity="warning", data=d, text="спред 1.2"))
    await n.on_event(ev("alert", seq=2, severity="warning", data=d, text="спред 1.3"))
    assert len(m.sent) == 1 and caller.calls == []


async def test_emergency_pause_without_yes_only_for_open_critical_alert(nw, paths):
    n, m, caller, ea, clock, paused, hub = nw
    data = {"key": "no_sl", "state": "open", "severity": "critical"}
    await n.on_event(ev("alert", severity="critical", data=data, text="позиция без стопа"))
    await asyncio.sleep(0.05)
    assert paused == ["pause"] and any("Поставил паузу сам" in x["outs"][0].text for x in m.sent)
    # если робот сам уже на паузе, второй раз не ставим
    paused.clear()
    ea.set["paused"] = True
    ea.write_status()
    await n.on_event(ev("alert", seq=3, data={"key": "orders_rejected", "state": "open", "severity": "critical"}, text="3 отказа"))
    await asyncio.sleep(0.05)
    assert paused == []
    # некритичная тревога паузу не ставит
    ea.set["paused"] = False
    ea.write_status()
    await n.on_event(ev("alert", seq=4, severity="warning", data={"key": "day_dd", "state": "open", "severity": "warning"}, text="-3%"))
    await asyncio.sleep(0.05)
    assert paused == []


async def test_ack_stops_calls(nw, db):
    n, m, caller, *_ = nw
    await n.on_event(ev("alert", severity="critical", data={"key": "algo_off", "state": "open", "severity": "critical"}, text="x"))
    assert not n.is_acked("algo_off")
    n.ack()
    assert n.is_acked("algo_off")
    await asyncio.sleep(0.05)
    assert (await db.alerts_open())[0]["acked"] == 1


async def test_robot_reports_are_not_duplicated_and_cmd_result_is_quiet(nw):
    n, m, *_ = nw
    await n.on_event(ev("morning_brief", text="☀️ План"))
    await n.on_event(ev("daily_report", text="📊 Итог"))
    await n.on_event(ev("cmd_result", data={"ok": True, "msg": "риск 0.5%"}))
    assert m.sent == []


async def test_chart_command_sends_screenshot(nw, paths):
    n, m, caller, ea, *_ = nw
    (paths.shots_dir / "7.png").write_bytes(b"\x89PNG-fake")
    await n.on_event(ev("cmd_result", data={"ok": True, "msg": "скрин H1", "shot": "7.png"}))
    assert m.sent[0]["outs"][0].photo == b"\x89PNG-fake"


async def test_internet_outage_queues_then_summarises(nw):
    n, m, caller, ea, clock, *_ = nw
    m.fail = True
    await n.on_event(ev("zone_confirmed", level=1, text="🟩 Зона BUY 2641.5"))
    await n.on_event(ev("limit_reached", seq=2, text="⛔ Дневной риск исчерпан"))
    assert len(n.backlog) == 2 and m.sent == []
    clock.t += timedelta(minutes=15)
    m.fail = False
    assert await n.flush_backlog() is True
    msg = m.texts[0]
    assert "Связи не было с 13:00 до 13:15" in msg and "Что пропустил" in msg and "Дневной риск" in msg
    assert n.backlog == []


# ---------------------------------------------------------------- сторож
class WdEnv:
    def __init__(self, nw):
        self.n, self.m, self.caller, self.ea, self.clock, self.paused, self.hub = nw
        self.running = True
        self.restarts = []
        self.free = 50.0
        s = self.n.s
        s.mt5_autorestart = True
        from agent.storage.db import Database
        self.wd = Watchdog(s, self.hub, self.n, self.n.db, is_running=lambda: self.running,
                           restart=lambda p: self.restarts.append(p), free_gb=lambda: self.free, clock=self.clock)


async def test_mt5_closed_alert_and_limited_restarts(nw):
    e = WdEnv(nw)
    await e.wd.check_once()                                    # процесс есть, всё тихо
    e.running = False
    await e.wd.check_once()                                    # только заметили
    assert not e.n.is_open("mt5_down")
    e.clock.t += timedelta(seconds=31)
    await e.wd.check_once()
    assert e.n.is_open("mt5_down") and len(e.restarts) == 1
    assert any("Перезапустил MT5" in t for t in e.m.texts)
    e.clock.t += timedelta(minutes=5)
    await e.wd.check_once()
    assert len(e.restarts) == 1                                # не чаще раза в 10 минут
    for _ in range(4):                                         # всего 49 минут от первого перезапуска
        e.clock.t += timedelta(minutes=11)
        await e.wd.check_once()
    assert len(e.restarts) == 3                                # и не больше 3 в час
    e.running = True
    await e.wd.check_once()
    assert not e.n.is_open("mt5_down")


async def test_robot_silent_when_status_is_stale(nw, paths):
    e = WdEnv(nw)
    st = json.loads(paths.status.read_text(encoding="utf-8"))
    st["ts_utc"] = "2020-01-01T00:00:00Z"
    paths.status.write_text(json.dumps(st))
    await e.wd.check_once()
    assert e.n.is_open("robot_silent") and "завис" in e.m.sent[0]["outs"][0].text
    e.ea.write_status()
    await e.wd.check_once()
    assert not e.n.is_open("robot_silent")


async def test_low_disk_warns_once_a_day(nw):
    e = WdEnv(nw)
    e.free = 0.4
    await e.wd.check_once()
    await e.wd.check_once()
    assert len([t for t in e.m.texts if "диске осталось" in t]) == 1


# ---------------------------------------------------------------- расписание
async def test_scheduler_runs_each_job_once_after_its_time(db):
    s = mk_settings()
    clock = Clock(datetime(2026, 11, 2, 3, 29, tzinfo=timezone.utc))     # 09:29 Бишкек, понедельник
    sch = Scheduler(s, db, clock)
    ran = []

    async def morning():
        ran.append("morning")

    async def weekly():
        ran.append("weekly")
    sch.add("morning", "09:30", WEEKDAYS, morning)
    sch.add_weekly("weekly", "Sat 10:00", weekly)
    assert await sch.tick() == []
    clock.t += timedelta(minutes=2)                                      # 09:31
    assert await sch.tick() == ["morning"] and await sch.tick() == []   # второй раз не срабатывает
    clock.t += timedelta(hours=1)
    assert await sch.tick() == []
    clock.t = datetime(2026, 11, 7, 4, 5, tzinfo=timezone.utc)           # суббота 10:05
    assert await sch.tick() == ["weekly"]


async def test_scheduler_skips_stale_job_after_grace_and_weekends(db):
    s = mk_settings()
    clock = Clock(datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc))      # 11:00: 90 минут после 09:30
    sch = Scheduler(s, db, clock)
    ran = []

    async def job():
        ran.append(1)
    sch.add("morning", "09:30", WEEKDAYS, job, grace_min=30)
    assert await sch.tick() == []                                        # устаревший план хуже тишины
    clock.t = datetime(2026, 11, 7, 3, 31, tzinfo=timezone.utc)          # суббота
    assert await sch.tick() == []


async def test_scheduler_failure_does_not_repeat_and_empty_time_disables(db):
    s = mk_settings()
    clock = Clock(datetime(2026, 11, 2, 3, 31, tzinfo=timezone.utc))
    sch = Scheduler(s, db, clock)
    n = []

    async def boom():
        n.append(1)
        raise RuntimeError("упал")
    sch.add("morning", "09:30", WEEKDAYS, boom)
    sch.add("off", "", WEEKDAYS, boom)
    await sch.tick()
    await sch.tick()
    assert n == [1]


def test_parse_weekly():
    assert parse_weekly("Sat 10:00")[0] == 5


# ---------------------------------------------------------------- отчёты
@pytest.fixture
async def rep(paths, db, ea):
    s = mk_settings()
    clock = Clock(datetime(2026, 11, 2, 17, 5, tzinfo=timezone.utc))     # 23:05 Бишкек
    hub = BridgeHub(paths, db, KEY)
    cm = ConfirmManager(db, make_executor(hub))
    ctx = ToolContext(s, db, hub, cm, engine=AnalysisEngine(FakeMT5Source(synthetic_m5(weeks=110, seed=11))), now=clock)
    ea.now = clock
    return ctx, ea, clock


def trade(ea, side, pts, reason):
    ea.set_price(2650.0)
    ea.open_position(side, 5.5, entry=2650.0)
    sign = 1 if side == "BUY" else -1
    return ea.close_position(reason, 2650.0 + sign * pts)


async def test_daily_report_numbers(rep):
    ctx, ea, clock = rep
    trade(ea, "BUY", 11.0, "TP")
    trade(ea, "SELL", 11.0, "TP")
    trade(ea, "BUY", -5.5, "SL")
    out = (await daily_report(ctx))[0].text
    assert "📊 Итог дня 02.11" in out and "Сделок 3/5: 2 TP, 1 SL" in out and "винрейт 67%" in out
    assert "+16.5 pt" in out and "Лучшая:" in out and "худшая:" in out and "Баланс:" in out
    assert "Дневной риск: потрачено" in out


async def test_daily_report_without_trades(rep):
    ctx, ea, clock = rep
    assert "Сделок сегодня не было" in (await daily_report(ctx))[0].text


async def test_morning_plan_and_evening_zones_use_engine(rep):
    ctx, ea, clock = rep
    out = await morning_plan(ctx)
    assert out[0].text.startswith("☀️ План на 02.11") and "Bias: D1" in out[0].text and "Сессия 10:00–23:00" in out[0].text
    assert out[1].photo and out[1].text.startswith("Золото ")
    ev_out = await evening_zones(ctx)
    assert ev_out[0].text.startswith("🌙 Зоны на завтра") and ev_out[0].photo


async def test_morning_plan_when_mt5_is_down_says_so(rep):
    ctx, ea, clock = rep
    ctx.engine.source.fail = True
    out = await morning_plan(ctx)
    assert "Зоны движка недоступны" in out[0].text and len(out) == 1


async def test_weekly_report_table_comparison_and_ai_review(rep, db):
    ctx, ea, clock = rep
    for i in range(6):
        trade(ea, "BUY", 11.0 if i % 2 else -5.5, "TP" if i % 2 else "SL")
    s = mk_settings()
    claude = FakeClaude([text_resp("Работают сделки по A. Стопы чаще в Азии.")])
    llm = LLM(s, db, client=claude, clock=clock)
    out = await weekly_report(ctx, llm, "СИСТЕМА")
    assert "Недельный разбор" in out[0].text and "profit factor" in out[0].text and "```" in out[0].text
    assert out[1].text.startswith("Работают сделки по A")
    assert claude.calls[0]["model"] == "claude-sonnet-5-5" and "tools" not in claude.calls[0]
    # бюджет исчерпан: таблица без разбора
    await db.add_usage(llm.budget.day(), "m", 1, 1, 0, 0, 9.0)
    out2 = await weekly_report(ctx, llm, "СИСТЕМА")
    assert len(out2) == 2 and "недоступен" in out2[1].text
