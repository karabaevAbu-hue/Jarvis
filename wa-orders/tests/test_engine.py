from collections import defaultdict
from datetime import datetime

from app.engine import day_start, in_quiet_hours, make_plan
from app.greenapi import GreenAPIError

from .conftest import TZ, add_groups


async def prepare(db, groups=3, orders=("Ташкент - Ош\nТент 120", "Москва - Бишкек", "Ош - Алматы")):
    await add_groups(db, groups)
    for slot, text in enumerate(orders, start=1):
        await db.set_order(slot, text, 1)


async def simulate(engine, clock, seconds):
    """Крутим движок как в жизни: шаг → ждём, сколько он попросил."""
    end = clock.now + seconds
    while clock.now < end:
        delay = await engine.step()
        clock.now += delay


def test_quiet_hours_overnight():
    night = datetime(2026, 11, 7, 2, 0, tzinfo=TZ).timestamp()
    morning = datetime(2026, 11, 7, 7, 30, tzinfo=TZ).timestamp()
    assert in_quiet_hours(night, TZ, "23:00", "07:00")
    assert not in_quiet_hours(morning, TZ, "23:00", "07:00")
    assert not in_quiet_hours(night, TZ, "00:00", "00:00")


def test_plan_matches_tz_numbers():
    # 20 живых по 15 мин + 30 тихих по 45 мин, пауза 20–30 с → 120/час при ёмкости 120
    plan = make_plan([15] * 20 + [45] * 30, 20, 30)
    assert round(plan.per_hour) == 120 and round(plan.capacity) == 120 and plan.factor == 1.0
    # 50 живых по 15 мин на одном номере — не успеваем
    assert make_plan([15] * 50, 20, 30).factor > 1.6


async def test_start_staggers_first_messages(db, engine, clock):
    await prepare(db, groups=3)
    await engine.start()
    times = sorted(g["next_send_at"] - clock.now for g in await db.enabled_groups())
    assert times == [0, 300, 600]


async def test_step_sends_summary_and_schedules_next(db, engine, api, clock):
    await prepare(db, groups=1)
    await engine.start()
    pause = await engine.step()
    assert 20 <= pause <= 30
    assert len(api.sent) == 1
    chat_id, text = api.sent[0]
    assert "1️⃣ Ташкент - Ош" in text and "3️⃣ Ош - Алматы" in text
    group = await db.get_group(chat_id)
    assert clock.now + 900 <= group["next_send_at"] <= clock.now + 900 * 1.15
    stats = await db.stats_since(0)
    assert stats == {"queued": 1}


async def test_two_hours_three_groups_acceptance(db, engine, api, clock):
    """Критерий приёмки: 3 группы, 15 минут, 2 часа → 7–8 сообщений в каждую, ни одного промежутка < 15 мин."""
    await prepare(db, groups=3)
    await engine.start()
    sends = defaultdict(list)
    original = api.send_message

    async def recording(chat_id, text):
        sends[chat_id].append(clock.now)
        return await original(chat_id, text)

    api.send_message = recording
    await simulate(engine, clock, 2 * 3600)
    assert len(sends) == 3
    for times in sends.values():
        assert 7 <= len(times) <= 8
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert min(gaps) >= 900


async def test_header_changes_between_sends_to_same_group(db, engine, api, clock):
    await prepare(db, groups=1)
    await engine.start()
    await simulate(engine, clock, 3 * 3600)
    headers = [text.split("\n")[0] for _, text in api.sent]
    assert len(headers) >= 10
    assert all(a != b for a, b in zip(headers, headers[1:]))


async def test_quiet_hours_block_sending(db, engine, api, clock):
    await prepare(db, groups=1)
    clock.now = datetime(2026, 11, 7, 23, 30, tzinfo=TZ).timestamp()
    await engine.start()
    assert await engine.step() == 60.0
    assert api.sent == []


async def test_daily_limit(db, engine, api, notifier, clock):
    await prepare(db, groups=3)
    await db.set("daily_limit", 2)
    await engine.start()
    await simulate(engine, clock, 3600)
    assert len(api.sent) == 2
    assert any("Дневной лимит" in m for m in notifier.messages)


async def test_waits_when_number_not_authorized(db, engine, api, runtime):
    await prepare(db, groups=1)
    await engine.start()
    runtime.wa_state = "sleepMode"
    assert await engine.step() == 10.0
    assert api.sent == []


async def test_no_orders_notifies_once(db, engine, api, notifier, clock):
    await add_groups(db, 1)
    await engine.start()
    await engine.step()
    await engine.step()
    assert api.sent == []
    assert sum("Нет активных заказов" in m for m in notifier.messages) == 1


async def test_removed_order_disappears_from_next_summary(db, engine, api, clock):
    await prepare(db, groups=1)
    await engine.start()
    await engine.step()
    await db.remove_order(2)
    clock.now += 900 * 1.15 + 1  # следующий слот не раньше чем через интервал +15%
    await engine.step()
    assert "Москва - Бишкек" in api.sent[0][1]
    assert "Москва - Бишкек" not in api.sent[1][1]


async def test_timeout_but_message_went_out_no_duplicate(db, engine, api, clock):
    await prepare(db, groups=1)
    await engine.start()
    chat_id = (await db.enabled_groups())[0]["chat_id"]
    error = GreenAPIError("sendMessage", body="timeout", network=True)
    error.went_out = True  # ответа не было, но сообщение есть в журнале исходящих
    api.fail_next = [error]
    await engine.step()
    for task in list(engine._background):
        await task
    assert await db.stats_since(0) == {"queued": 1}
    assert api.sent == []
    assert (await db.get_group(chat_id))["next_send_at"] >= clock.now + 900  # повтора нет


async def test_timeout_and_not_sent_retries_once(db, engine, api, clock):
    await prepare(db, groups=1)
    await engine.start()
    chat_id = (await db.enabled_groups())[0]["chat_id"]
    api.fail_next = [GreenAPIError("sendMessage", body="timeout", network=True)]
    await engine.step()
    for task in list(engine._background):
        await task
    assert (await db.get_group(chat_id))["next_send_at"] == clock.now
    await engine.step()
    assert len(api.sent) == 1
    assert await db.stats_since(0) == {"not_sent": 1, "queued": 1}


async def test_timeout_but_message_still_in_queue_no_duplicate(db, engine, api, clock):
    """ТЗ, раздел 6: после таймаута сверяем журнал И очередь. Сообщение ещё в очереди — повтор дал бы дубль."""
    await prepare(db, groups=1)
    await engine.start()
    chat_id = (await db.enabled_groups())[0]["chat_id"]

    async def timeout_after_enqueue(target, text):
        api.queue.append({"body": {"chatId": target, "message": text}, "type": "sendMessage"})
        raise GreenAPIError("sendMessage", body="timeout", network=True)

    api.send_message = timeout_after_enqueue
    await engine.step()
    for task in list(engine._background):
        await task
    assert await db.stats_since(0) == {"queued": 1}
    assert (await db.get_group(chat_id))["next_send_at"] >= clock.now + 900  # повтора нет


async def test_three_failures_disable_group(db, engine, api, notifier, clock):
    await prepare(db, groups=1)
    await engine.start()
    chat_id = (await db.enabled_groups())[0]["chat_id"]
    for _ in range(3):
        api.fail_next = [GreenAPIError("sendMessage", 500, "boom")]
        await db.set_next_send(chat_id, clock.now)
        await engine.step()
    assert (await db.get_group(chat_id))["status"] == "error"
    assert any("3 ошибки подряд" in m for m in notifier.messages)


async def test_five_failures_in_a_row_autopause(db, engine, api, notifier, clock):
    await prepare(db, groups=5)
    await engine.start()
    for group in await db.enabled_groups():
        await db.set_next_send(group["chat_id"], clock.now)
    api.fail_next = [GreenAPIError("sendMessage", 500, "boom") for _ in range(5)]
    for _ in range(5):
        await engine.step()
    assert await db.get("running") == "0"
    assert api.cleared == 1
    assert any("Автопауза" in m for m in notifier.messages)


async def test_quota_error_stops(db, engine, api, notifier):
    await prepare(db, groups=1)
    await engine.start()
    api.fail_next = [GreenAPIError("sendMessage", 466, "limit")]
    await engine.step()
    assert await db.get("running") == "0"
    assert any("лимит тарифа" in m for m in notifier.messages)


async def test_stale_sending_marked_unknown_on_restart(db, engine, api, clock):
    await prepare(db, groups=1)
    chat_id = (await db.enabled_groups())[0]["chat_id"]
    await db.log_sending(chat_id, clock.now, "текст")
    assert await db.mark_stale_sending() == 1
    assert await db.stats_since(0) == {"unknown": 1}


async def test_stop_all_clears_queue(db, engine, api):
    await prepare(db, groups=1)
    await engine.start()
    api.queue = [1, 2, 3]
    assert await engine.stop_all() == 0
    assert await db.get("running") == "0"
    assert all(g["next_send_at"] is None for g in await db.enabled_groups())


async def test_day_start_is_local_midnight(clock):
    assert datetime.fromtimestamp(day_start(clock.now, TZ), TZ).strftime("%H:%M") == "00:00"
