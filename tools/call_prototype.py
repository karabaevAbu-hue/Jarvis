"""Шаг 2 плана: прототип звонка. Запускается на ПК Абу (Windows), в облаке проверить его нельзя.

    python -m tools.call_prototype listen   # Абу звонит ассистенту: принять, сказать фразу, записать 5 с голоса
    python -m tools.call_prototype call     # ассистент звонит Абу: сказать фразу

Проверяет ровно тот адаптер, на котором работает ассистент (agent.tg.calls.PyTgCallsBackend), поэтому
результат решает, оставляем ли py-tgcalls и Pyrogram-форк или переходим на запасной вариант (раздел 20 ТЗ):
приватная группа из двух с голосовым чатом, либо только голосовые сообщения.

Итог печатается по-русски: что сработало, что нет, сколько кадров и с какой частотой пришло.
"""
from __future__ import annotations

import asyncio
import sys
import time

from agent.config import get_settings
from agent.speech.stt import write_wav
from agent.speech.tts import TTS
from agent.speech.vad import resample_48k_to_16k

PHRASE = "Слушаю, Абу. Это проверка звонка."
RECORD_SEC = 5


async def main(mode: str) -> int:
    s = get_settings()
    if not (s.tg_api_id and s.tg_api_hash and s.owner_id):
        print("Заполни TG_API_ID, TG_API_HASH, OWNER_ID в .env")
        return 2
    from pyrogram import Client
    from agent.tg.calls import PyTgCallsBackend

    app = Client("consigliere", api_id=s.tg_api_id, api_hash=s.tg_api_hash, workdir=s.data_dir)
    backend = PyTgCallsBackend(app)
    tts = TTS(s.voice, f"{s.data_dir}/cache/tts")
    pcm = await tts.pcm48k(PHRASE)
    if pcm is None:
        print("✖ озвучка недоступна (edge-tts и голос Windows): проверь интернет и ffmpeg")
        return 3

    got = bytearray()
    t_first: list[float] = []
    ended = asyncio.Event()

    def on_audio(chunk: bytes) -> None:
        if not t_first:
            t_first.append(time.monotonic())
        got.extend(chunk)

    backend.on_audio(on_audio)
    backend.on_ended(lambda: ended.set())

    async def on_incoming(user_id: int) -> None:
        if user_id != s.owner_id:
            print(f"чужой звонок от {user_id}: отклоняю")
            await backend.decline(user_id)
            return
        print("входящий звонок от владельца: принимаю")
        await backend.accept(user_id)
        await dialog(user_id)

    async def dialog(user_id: int) -> None:
        await backend.play_pcm(user_id, pcm)
        t0 = time.monotonic()
        while time.monotonic() - t0 < RECORD_SEC and not ended.is_set():
            await asyncio.sleep(0.1)
        await backend.hangup(user_id)
        ended.set()

    backend.on_incoming(on_incoming)
    await app.start()
    await backend.start()
    ok = False
    try:
        if mode == "listen":
            print(f"Жду звонок от {s.owner_id}. Позвони ассистенту из Telegram и скажи что-нибудь 5 секунд.")
            await asyncio.wait_for(ended.wait(), timeout=300)
        else:
            print("Звоню владельцу, возьми трубку.")
            if await backend.place(s.owner_id, 30):
                await dialog(s.owner_id)
            else:
                print("✖ звонок не принят (не ответил или библиотека не смогла позвонить)")
                return 4
        secs = len(got) / (48000 * 2)
        write_wav(f"{s.data_dir}/proto_in.wav", resample_48k_to_16k(bytes(got)), 16000)
        ok = secs > 1.0
        print(f"{'✔' if ok else '✖'} принято звука: {secs:.1f} с (ожидали около {RECORD_SEC}), "
              f"файл {s.data_dir}/proto_in.wav: послушай, слышен ли голос")
        print("Итог:", "py-tgcalls + Pyrogram-форк подходят" if ok else
              "звук не пришёл: смотри лог, при неудаче берём запасной вариант (раздел 20 ТЗ)")
    finally:
        await app.stop()
    return 0 if ok else 5


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("listen", "call"):
        print(__doc__)
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1])))
