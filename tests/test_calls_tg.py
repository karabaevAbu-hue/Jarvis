"""Звонки (на двойнике backend) и Telegram-слой: охрана владельца, очередь, FloodWait, повторы тревоги."""
import asyncio
import time
from types import SimpleNamespace as NS

import pytest

from agent.brain.tools import OutMsg
from agent.speech.vad import EnergyVAD, Endpointer
from agent.tg.calls import AlertCaller, CallSession
from agent.tg.client import FloodWait, Messenger, OwnerGuard, SendQueue, chunk_text
from tests.test_speech import SR_CALL, silence, tone

OWNER = 777


# ---------------------------------------------------------------- двойники
class FakeBackend:
    def __init__(self, answers=None, play_delay=0.0):
        self.answers = list(answers or [])
        self.play_delay = play_delay
        self.played, self.stopped, self.hung, self.placed, self.declined = [], 0, 0, 0, []

    async def place(self, user_id, timeout):
        self.placed += 1
        return self.answers.pop(0) if self.answers else False

    async def play_pcm(self, user_id, pcm):
        self.played.append(len(pcm))
        await asyncio.sleep(self.play_delay)

    async def stop_playback(self, user_id):
        self.stopped += 1

    async def hangup(self, user_id):
        self.hung += 1

    async def decline(self, user_id):
        self.declined.append(user_id)


class FakeTTS:
    def __init__(self):
        self.texts = []

    async def pcm48k(self, text):
        self.texts.append(text)
        return b"\x01\x00" * 480


class FakeSTT:
    def __init__(self, *texts, conf=0.95):
        self.texts, self.conf = list(texts), conf

    async def transcribe_pcm16k(self, pcm):
        return NS(text=self.texts.pop(0) if self.texts else "", confidence=self.conf)


class FakeAssistant:
    def __init__(self, reply="Работает.", delay=0.0):
        self.reply, self.delay, self.calls = reply, delay, []

    async def handle(self, text, **kw):
        self.calls.append((text, kw))
        await asyncio.sleep(self.delay)
        return self.reply if isinstance(self.reply, list) else [OutMsg(self.reply)]


def mk_session(backend=None, assistant=None, stt=None, **kw):
    backend = backend or FakeBackend()
    chat = []

    async def send_chat(outs):
        chat.extend(outs)

    s = CallSession(backend, assistant or FakeAssistant(), stt or FakeSTT("как дела"), FakeTTS(),
                    Endpointer(EnergyVAD()), send_chat, lambda: "Робот работает, сделок 2 из 5.",
                    silence_sec=kw.pop("silence_sec", 5), **kw)
    return s, backend, chat


async def talk(s, *chunks, chunk_ms=20):
    step = SR_CALL * chunk_ms // 1000 * 2
    for pcm in chunks:
        for i in range(0, len(pcm), step):
            s.feed_audio(pcm[i:i + step])
        await asyncio.sleep(0)


# ---------------------------------------------------------------- диалог в звонке
async def test_greeting_then_question_answer_and_latency():
    ast = FakeAssistant("Работает. Сегодня две сделки.")
    s, b, chat = mk_session(assistant=ast, stt=FakeSTT("как дела у робота"))
    task = asyncio.create_task(s.run(OWNER))
    await asyncio.sleep(0.3)                                      # приветствие прозвучало
    assert s.tts.texts[0] == "Слушаю, Абу."
    assert "Робот работает" in s.tts.texts[1]
    await talk(s, silence(0.3), tone(1.0), silence(1.0))
    await asyncio.sleep(0.5)
    text, kw = ast.calls[0]
    assert text == "как дела у робота" and kw["voice"] is True    # звонок идёт в режиме voice
    assert any("Работает" in t for t in s.tts.texts)
    assert len(s.latencies) == 1 and s.latencies[0] < 4.0          # замер: конец фразы -> первый звук
    s.mark_ended()
    assert await asyncio.wait_for(task, 3) == "ended" and b.hung == 1


async def test_barge_in_stops_assistant_speech():
    b = FakeBackend(play_delay=2.0)                                # длинная реплика
    s, b, chat = mk_session(backend=b)
    task = asyncio.create_task(s.run(OWNER))
    await asyncio.sleep(0.2)                                       # приветствие ещё играет
    await talk(s, silence(0.2), tone(0.4))                         # Абу перебил
    await asyncio.sleep(0.3)
    assert b.stopped >= 1 and s._speaking.done()
    s.mark_ended()
    await asyncio.wait_for(task, 3)


async def test_silence_60s_hangs_up_with_phrase():
    s, b, chat = mk_session(silence_sec=0.4)
    reason = await asyncio.wait_for(s.run(OWNER), 5)
    assert reason == "silence" and "Если что, я на связи." in s.tts.texts and b.hung == 1


async def test_call_limit():
    s, b, chat = mk_session(silence_sec=50, max_minutes=0.005)
    assert await asyncio.wait_for(s.run(OWNER), 5) == "limit"


async def test_zone_answer_goes_to_chat_and_voice_says_so():
    zones = [OutMsg("Золото 2650.30 · зоны ...", photo=b"\x89PNG"), OutMsg("Главное: SELL сверху.")]
    s, b, chat = mk_session(assistant=FakeAssistant(zones), stt=FakeSTT("дай зону"))
    task = asyncio.create_task(s.run(OWNER))
    await asyncio.sleep(0.2)
    await talk(s, silence(0.3), tone(1.0), silence(1.0))
    await asyncio.sleep(0.6)
    assert len(chat) == 1 and chat[0].photo                       # график и зоны ушли в чат
    assert "Прислал текстом и графиком в чат." in " ".join(s.tts.texts)
    s.mark_ended()
    await asyncio.wait_for(task, 3)


async def test_slow_model_gets_filler_phrase():
    s, b, chat = mk_session(assistant=FakeAssistant("Готово.", delay=0.6), filler_after=0.1)
    task = asyncio.create_task(s.run(OWNER))
    await asyncio.sleep(0.2)
    await talk(s, silence(0.3), tone(1.0), silence(1.0))
    await asyncio.sleep(1.0)
    assert "Сейчас смотрю." in s.tts.texts
    s.mark_ended()
    await asyncio.wait_for(task, 3)


async def test_unsure_voice_confidence_is_passed_to_assistant():
    ast = FakeAssistant("Не уверен.")
    s, b, chat = mk_session(assistant=ast, stt=FakeSTT("да", conf=0.3))
    task = asyncio.create_task(s.run(OWNER))
    await asyncio.sleep(0.2)
    await talk(s, silence(0.3), tone(0.6), silence(1.0))
    await asyncio.sleep(0.4)
    assert ast.calls[0][1]["confidence"] == 0.3                    # confirm.py по нему переспросит
    s.mark_ended()
    await asyncio.wait_for(task, 3)


# ---------------------------------------------------------------- звонок по тревоге
def mk_caller(backend, **kw):
    sent = {"text": [], "voice": [], "sleeps": []}

    async def send_text(t):
        sent["text"].append(t)

    async def send_voice(t):
        sent["voice"].append(t)

    async def fake_sleep(sec):
        sent["sleeps"].append(sec)

    sessions = []

    class Sess:
        async def run(self, user_id, greeting=None):
            sessions.append(greeting)
            return "ended"

    c = AlertCaller(backend, OWNER, lambda: Sess(), send_text, send_voice,
                    kw.pop("is_open", lambda k: True), kw.pop("is_acked", lambda k: False), sleep=fake_sleep, **kw)
    return c, sent, sessions


async def test_alert_call_retries_until_answered():
    b = FakeBackend(answers=[False, False, True])
    c, sent, sessions = mk_caller(b)
    assert await c.call("no_sl", "Позиция без стопа", "🚨 Позиция без стопа") == "answered"
    assert b.placed == 3 and sessions == ["Позиция без стопа"]       # в звонке сначала сама тревога
    assert sent["voice"] == ["Позиция без стопа"] and sent["text"][0].startswith("🚨")   # голосовое + текст один раз
    assert sent["sleeps"] == [120, 120]                              # повтор через 2 минуты


async def test_alert_gives_up_after_five_attempts():
    b = FakeBackend()
    c, sent, _ = mk_caller(b)
    assert await c.call("disconnect", "Нет связи", "🚨 Нет связи") == "gave_up"
    assert b.placed == 5 and len(sent["sleeps"]) == 4


async def test_alert_stops_when_acked_or_closed():
    b = FakeBackend()
    state = {"acked": False, "open": True}
    c, sent, _ = mk_caller(b, is_open=lambda k: state["open"], is_acked=lambda k: state["acked"])

    async def sleeper(sec):
        state["acked"] = True                                        # Абу написал «принял»
    c.sleep = sleeper
    assert await c.call("no_sl", "x", "x") == "acked" and b.placed == 1

    b2 = FakeBackend()
    st2 = {"open": True}
    c2, _, _ = mk_caller(b2, is_open=lambda k: st2["open"])

    async def sleeper2(sec):
        st2["open"] = False                                          # причина ушла
    c2.sleep = sleeper2
    assert await c2.call("no_sl", "x", "x") == "closed" and b2.placed == 1


# ---------------------------------------------------------------- Telegram-слой
async def test_owner_guard_ignores_strangers_and_notifies_once_a_day(db):
    notes = []

    async def notify(t):
        notes.append(t)
    g = OwnerGuard(OWNER, db, notify)
    assert g.allowed(OWNER) and not g.allowed(123) and not g.allowed(None)
    await g.stranger(123, "spam_bot")
    await g.stranger(124, None, "звонок")
    assert len(notes) == 1 and "@spam_bot" in notes[0]               # одно уведомление в сутки


def test_guard_denies_everyone_when_owner_not_configured(db):
    g = OwnerGuard(0, db, None)  # type: ignore[arg-type]
    assert not g.allowed(0) and not g.allowed(5)


async def test_send_queue_one_message_per_second_and_floodwait():
    t = {"now": 0.0}
    slept = []

    async def sleep(sec):
        slept.append(sec)
        t["now"] += sec
    q = SendQueue(min_interval=1.0, sleep=sleep, clock=lambda: t["now"])
    sent = []

    async def ok(n):
        sent.append(n)
        return n
    r = [await q.submit(lambda i=i: ok(i)) for i in range(3)]
    assert r == [0, 1, 2] and all(abs(x - 1.0) < 1e-9 for x in slept[-2:])     # не чаще 1 сообщения в секунду

    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise FloodWait(7)
        return "ok"
    assert await q.submit(flaky) == "ok" and 8 in slept              # ждали ровно сколько сказано (+1 с запаса)
    await q.stop()


def test_chunk_text_respects_limit():
    parts = chunk_text("строка\n" * 1500, 4000)
    assert len(parts) >= 3 and all(len(p) <= 4000 for p in parts)


class FakeTransport:
    def __init__(self):
        self.sent = []

    async def send_text(self, text, reply_to, silent):
        self.sent.append(("text", text, reply_to, silent))
        return len(self.sent)

    async def send_photo(self, png, caption, reply_to, silent):
        self.sent.append(("photo", caption, reply_to, silent))
        return len(self.sent)

    async def send_voice(self, ogg, reply_to, silent):
        self.sent.append(("voice", None, reply_to, silent))
        return len(self.sent)


async def test_messenger_caption_rule_and_threading():
    tr = FakeTransport()
    m = Messenger(tr, SendQueue(min_interval=0))
    await m.photo(b"x", "короткая подпись", reply_to=5, silent=True)
    await m.photo(b"x", "д" * 1500)                                  # длиннее 1024: фото, потом текст
    kinds = [s[0] for s in tr.sent]
    assert kinds == ["photo", "photo", "text"] and tr.sent[0][2] == 5 and tr.sent[0][3] is True
    assert tr.sent[1][1] == "" and len(tr.sent[2][1]) == 1500
    await m.q.stop()
