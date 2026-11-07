import asyncio

from app.watchdog import Watchdog, ensure_instance_settings

from .conftest import add_groups

_REAL_SLEEP = asyncio.sleep  # в тестах ниже asyncio.sleep подменяется, ждать условия надо настоящим


async def wait_until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return True
        await _REAL_SLEEP(0.005)
    return False


def make_watchdog(api, db, engine, notifier, runtime, clock):
    return Watchdog(api, db, engine, notifier, runtime, clock=clock, poll_seconds=0)


async def test_settings_applied_only_when_different(api):
    api.settings = {"webhookUrl": None, "outgoingWebhook": "yes", "delaySendMessagesMilliseconds": 5000}
    assert await ensure_instance_settings(api) is True
    first = api.set_calls[0]
    assert first["delaySendMessagesMilliseconds"] == 10000 and "outgoingWebhook" not in first
    assert first["incomingWebhook"] == "no"
    assert await ensure_instance_settings(api) is False


async def test_first_authorized_is_silent(api, db, engine, notifier, runtime, clock):
    runtime.wa_state = "unknown"
    wd = make_watchdog(api, db, engine, notifier, runtime, clock)
    await wd.apply_state("authorized")
    assert runtime.wa_state == "authorized" and notifier.messages == []


async def test_blocked_stops_and_clears_queue(api, db, engine, notifier, runtime, clock):
    await add_groups(db, 1)
    await db.set_order(1, "Заказ", 1)
    await engine.start()
    wd = make_watchdog(api, db, engine, notifier, runtime, clock)
    await wd.apply_state("blocked")
    assert await db.get("running") == "0"
    assert api.cleared == 1
    assert any("ЗАБАНЕН" in m for m in notifier.messages)


async def test_not_authorized_clears_queue_but_keeps_running_flag(api, db, engine, notifier, runtime, clock):
    await add_groups(db, 1)
    await db.set_order(1, "Заказ", 1)
    await engine.start()
    wd = make_watchdog(api, db, engine, notifier, runtime, clock)
    await wd.apply_state("notAuthorized")
    assert api.cleared == 1
    assert await db.get("running") == "1"  # вернётся сам после переподключения
    await wd.apply_state("authorized")
    assert any("снова в сети" in m for m in notifier.messages)
    assert all(g["next_send_at"] is not None for g in await db.enabled_groups())


async def test_long_starting_triggers_single_reboot(api, db, engine, notifier, runtime, clock):
    wd = make_watchdog(api, db, engine, notifier, runtime, clock)
    await wd.apply_state("starting")
    clock.now += 301
    await wd.apply_state("starting")
    clock.now += 400
    await wd.apply_state("starting")
    assert api.rebooted == 1
    assert sum("дольше 5 минут" in m for m in notifier.messages) == 1


async def test_not_in_group_status_disables_group(api, db, engine, notifier, runtime, clock):
    await add_groups(db, 1)
    chat_id = (await db.enabled_groups())[0]["chat_id"]
    log_id = await db.log_sending(chat_id, clock.now, "текст")
    await db.log_update(log_id, "queued", id_message="M1")
    wd = make_watchdog(api, db, engine, notifier, runtime, clock)
    await wd.handle({"typeWebhook": "outgoingMessageStatus", "idMessage": "M1", "status": "notInGroup"})
    assert (await db.get_group(chat_id))["status"] == "removed"
    assert any("не состоит в группе" in m for m in notifier.messages)


async def test_delivery_statuses_update_log(api, db, engine, notifier, runtime, clock):
    await add_groups(db, 1)
    chat_id = (await db.enabled_groups())[0]["chat_id"]
    log_id = await db.log_sending(chat_id, clock.now, "текст")
    await db.log_update(log_id, "queued", id_message="M2")
    wd = make_watchdog(api, db, engine, notifier, runtime, clock)
    await wd.handle({"typeWebhook": "outgoingMessageStatus", "idMessage": "M2", "status": "delivered"})
    assert await db.stats_since(0) == {"delivered": 1}


async def test_state_loop_survives_unexpected_error(api, db, engine, notifier, runtime, clock):
    """Сторож не должен молча умирать от неожиданного сбоя: иначе бан номера останется незамеченным."""
    calls = 0

    async def flaky_state():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("неожиданный ответ")
        return "blocked"

    api.get_state = flaky_state
    task = asyncio.create_task(make_watchdog(api, db, engine, notifier, runtime, clock).run_state())
    try:
        assert await wait_until(lambda: runtime.wa_state == "blocked")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_notifications_loop_survives_unexpected_error(api, db, engine, notifier, runtime, clock, monkeypatch):
    async def no_wait(delay, *args):  # паузы повтора (10–30 с) в тесте не ждём
        await _REAL_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    calls, deleted = 0, []

    async def flaky_receive(wait_seconds=20):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("неожиданный ответ")
        if calls == 2:
            return {"receiptId": 7, "body": {"typeWebhook": "stateInstanceChanged", "stateInstance": "blocked"}}
        await _REAL_SLEEP(0.005)  # дальше уведомлений нет
        return None

    async def delete_notification(receipt_id):
        deleted.append(receipt_id)

    api.receive_notification = flaky_receive
    api.delete_notification = delete_notification
    task = asyncio.create_task(make_watchdog(api, db, engine, notifier, runtime, clock).run_notifications())
    try:
        assert await wait_until(lambda: deleted == [7])
        assert runtime.wa_state == "blocked"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_sync_groups_counts_new_and_gone(db):
    assert await db.sync_groups([("a@g.us", "A"), ("b@g.us", "B")]) == (2, 0)
    assert await db.sync_groups([("a@g.us", "A2"), ("c@g.us", "C")]) == (1, 1)
    groups = {g["chat_id"]: g for g in await db.list_groups()}
    assert groups["b@g.us"]["status"] == "removed" and groups["a@g.us"]["name"] == "A2"
    assert await db.sync_groups([("b@g.us", "B")]) == (0, 2)
    assert (await db.get_group("b@g.us"))["status"] == "ok"
