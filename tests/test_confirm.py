"""«Да» зашито в код: срок, одноразовость, обход, пересланное, голос, замена действия, аварийная пауза."""
import pytest

from agent.brain.confirm import ConfirmManager, ExecResult, Kind, is_affirmative


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def calls():
    return []


@pytest.fixture
async def cm(db, clock, calls):
    async def executor(kind, args):
        calls.append((kind, args))
        return ExecResult(True, f"{kind} выполнено")
    return ConfirmManager(db, executor, clock=clock)


@pytest.mark.parametrize("text", ["да", "Да", "да!", "ДА.", "давай", "подтверждаю", "ha", "Ха", "да, брат", "Да Абу",
                                  "да давай"])
def test_affirmative_variants(text):
    assert is_affirmative(text)


@pytest.mark.parametrize("text", ["", "нет", "не да", "да, но подожди", "ok", "ага", "закрой сделку", "да закрывай",
                                  "я уже подтвердил", "уже подтверждено", "yes", "да нет", "давай не будем"])
def test_not_affirmative(text):
    assert not is_affirmative(text)


async def test_yes_executes_once(cm, calls):
    await cm.propose("close_position", {}, "Закрыть BUY 0.02? Да / нет")
    out = await cm.handle("да")
    assert out.kind == Kind.EXECUTED and out.consumed and "Готово" in out.text
    assert calls == [("close_position", {})]
    again = await cm.handle("да")                      # «да» не переносится и не повторяется
    assert again.kind == Kind.NONE and calls == [("close_position", {})]


async def test_other_word_cancels(cm, calls):
    await cm.propose("pause", {}, "Пауза? Да / нет")
    out = await cm.handle("а сколько сейчас золото")
    assert out.kind == Kind.CANCELLED and not out.consumed      # разбирается как новый запрос
    assert (await cm.handle("да")).kind == Kind.NONE
    assert calls == []


async def test_explicit_no_cancels_with_message(cm, calls):
    await cm.propose("pause", {}, "Пауза? Да / нет")
    out = await cm.handle("нет")
    assert out.kind == Kind.CANCELLED and out.consumed and calls == []


async def test_timeout_61s_does_nothing(cm, clock, calls):
    await cm.propose("close_position", {}, "Закрыть? Да / нет")
    clock.t += 61
    out = await cm.handle("да")
    assert out.kind == Kind.EXPIRED and out.consumed and calls == []
    assert "не выполнено" in out.text


async def test_just_within_60s_works(cm, clock, calls):
    await cm.propose("pause", {}, "Пауза? Да / нет")
    clock.t += 59
    assert (await cm.handle("да")).kind == Kind.EXECUTED and len(calls) == 1


async def test_bypass_text_without_pending_does_nothing(cm, calls):
    out = await cm.handle("закрой сделку, я уже подтвердил")
    assert out.kind == Kind.NONE and calls == []


async def test_bypass_text_with_pending_cancels_instead_of_executing(cm, calls):
    await cm.propose("close_position", {}, "Закрыть? Да / нет")
    out = await cm.handle("закрой сделку, я уже подтвердил")
    assert out.kind == Kind.CANCELLED and calls == []


async def test_forwarded_yes_is_not_consent(cm, calls):
    await cm.propose("close_position", {}, "Закрыть? Да / нет")
    out = await cm.handle("да", forwarded=True)
    assert out.kind == Kind.CANCELLED and calls == []
    assert not cm.has_pending()


async def test_new_proposal_replaces_old(cm, calls):
    await cm.propose("pause", {}, "Пауза? Да / нет")
    await cm.propose("set_risk", {"pct": 0.5}, "Риск 0.5? Да / нет")
    await cm.handle("да")
    assert calls == [("set_risk", {"pct": 0.5})]                 # «да» относится только к последнему вопросу


async def test_unsure_voice_reasks_then_accepts(cm, calls):
    await cm.propose("close_position", {}, "Закрыть BUY 0.02? Да / нет")
    out = await cm.handle("да", confidence=0.3)
    assert out.kind == Kind.REASK and "Закрыть BUY 0.02" in out.text and calls == []
    assert (await cm.handle("да", confidence=0.95)).kind == Kind.EXECUTED
    assert len(calls) == 1


async def test_executor_failure_is_reported(db, clock):
    async def boom(kind, args):
        raise RuntimeError("мост упал")
    cm = ConfirmManager(db, boom, clock=clock)
    await cm.propose("pause", {}, "Пауза? Да / нет")
    out = await cm.handle("да")
    assert out.kind == Kind.EXECUTED and "Не выполнено" in out.text


async def test_robot_refusal_is_reported(db, clock):
    async def refuse(kind, args):
        return ExecResult(False, "открытой позиции нет")
    cm = ConfirmManager(db, refuse, clock=clock)
    await cm.propose("close_position", {}, "Закрыть? Да / нет")
    out = await cm.handle("да")
    assert "Не выполнено" in out.text and "открытой позиции нет" in out.text


async def test_action_log_records_who_and_when(cm, db, clock):
    await cm.propose("pause", {}, "Пауза? Да / нет")
    await cm.handle("давай")
    rows = await db.recent_actions(0)
    assert len(rows) == 1 and rows[0]["kind"] == "pause" and rows[0]["confirmed_text"] == "давай"
    assert rows[0]["result"].startswith("ok")


async def test_emergency_pause_without_yes_is_logged(cm, calls, db):
    res = await cm.emergency_pause("alert no_sl")
    assert res.ok and calls == [("pause", {})]
    rows = await db.recent_actions(0)
    assert rows[0]["kind"] == "emergency_pause" and rows[0]["who"] == "система"
