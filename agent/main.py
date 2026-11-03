"""Запуск ассистента.

    start_agent.bat --login      первый вход аккаунта ассистента (код из Telegram + пароль 2FA)
    start_agent.bat --selftest   проверка Telegram, моста, MT5, Claude (--no-send: без тестового сообщения)
    start_agent.bat              работа
    python -m agent.main --console [--fake-robot]   диалог в терминале без Telegram (разработка и проверка)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from agent.config import Settings, get_settings
from agent.runtime import Runtime, find_robot

log = logging.getLogger("main")


def setup_logging(data_dir: str) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(Path(data_dir) / "assistant.log", maxBytes=2_000_000, backupCount=3,
                                              encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    # ключи и хэши в лог не попадают: библиотеки не логируем подробнее INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)


class ConsoleTransport:
    """Вместо Telegram: печатает ответы, картинки складывает в data/console/."""

    def __init__(self, data_dir: str):
        self.dir = Path(data_dir) / "console"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    async def send_text(self, text, reply_to, silent):
        self.n += 1
        print(f"\n[Консильери] {text}\n")
        return self.n

    async def send_photo(self, png, caption, reply_to, silent):
        self.n += 1
        f = self.dir / f"{self.n}.png"
        f.write_bytes(png)
        print(f"\n[Консильери] (график: {f})\n{caption}\n")
        return self.n

    async def send_voice(self, ogg, reply_to, silent):
        self.n += 1
        print("\n[Консильери] (голосовое)\n")
        return self.n


def build_stt_tts(s: Settings):
    """Голос выключен (VOICE_ENABLED=false, по умолчанию): (None, None), ни whisper, ни torch не импортируются."""
    if not s.voice_enabled:
        return None, None
    from agent.speech.stt import STT
    from agent.speech.tts import TTS
    return (STT(s.stt_model, s.stt_device, str(Path(s.data_dir) / "models")),
            TTS(s.voice, Path(s.data_dir) / "cache" / "tts"))


# ------------------------------------------------------------------ режимы
async def run_console(s: Settings, fake_robot: bool, real_mt5: bool) -> int:
    from agent.storage.db import Database
    from agent.tg.client import Messenger, SendQueue
    from tools.fake_ea import FakeEA
    from tools.fake_mt5 import FakeMT5Source
    from agent.bridge.protocol import BridgePaths

    s.owner_id = s.owner_id or 1
    s.bridge_key = s.bridge_key or "console-key"
    db = await Database(s.db_path).open()
    if real_mt5:
        from agent.analysis.data_mt5 import MT5Source
        source = MT5Source(s.mt5_path)
    else:
        source = FakeMT5Source()
    stt, tts = build_stt_tts(s)
    rt = Runtime(s, db, Messenger(ConsoleTransport(s.data_dir), SendQueue(min_interval=0)), source, tts, stt)
    ea = None
    if fake_robot:
        s.bridge_root = s.bridge_root or str(Path(s.data_dir) / "bridge")
        ea = FakeEA(BridgePaths(Path(s.bridge_root), "12345678_777001"), s.bridge_key)
        ea.start()
    await rt.start()
    if ea:
        await rt.wait_robot(10)

    async def robot():
        while ea:
            ea.tick(status_every=5)
            await asyncio.sleep(1)
    task = asyncio.create_task(robot())
    print("Режим консоли. Пиши как Абу. Пустая строка или Ctrl+D — выход.")
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line.strip():
                break
            await rt.on_message(s.owner_id, "console", text=line.strip())
    finally:
        task.cancel()
        await rt.stop()
        await db.close()
    return 0


async def run_login(s: Settings) -> int:
    from agent.tg.client import PyrogramTransport
    if not (s.tg_api_id and s.tg_api_hash):
        print("Заполни TG_API_ID и TG_API_HASH в .env (my.telegram.org → API development tools).")
        return 2
    tg = PyrogramTransport(s.tg_api_id, s.tg_api_hash, s.tg_phone, s.owner_id, s.data_dir)
    await tg.start()                      # спросит код из Telegram и пароль 2FA
    me = await tg.app.get_me()
    print(f"Вход выполнен: {me.first_name} (id {me.id}). Сессия сохранена в {s.data_dir}. Файл сессии никому не передавать.")
    await tg.stop()
    return 0


async def run_selftest(s: Settings, send_test: bool = True) -> int:
    """Проверки по отдельности: настройки, Telegram (вход, владелец, отправка), мост (папка, статус, ping), MT5, Claude."""
    from agent import selftest as st
    from agent.analysis.data_mt5 import MT5Source
    from agent.bridge.hub import BridgeHub
    from agent.brain.llm import LLM
    from agent.storage.db import Database
    from agent.tg.client import PyrogramProbe

    db = await Database(s.db_path).open()
    source = MT5Source(s.mt5_path)
    paths = find_robot(s)
    hub = BridgeHub(paths, db, s.bridge_key) if paths else None
    tg = st.TelegramCheck(s, lambda: PyrogramProbe(s.tg_api_id, s.tg_api_hash, s.tg_phone, s.data_dir),
                          send_test=send_test)
    checks = [("Настройки", lambda: st.check_config(s)), ("Диск", lambda: st.check_disk(s)),
              ("Telegram: вход", tg.login), ("Telegram: владелец", tg.owner), ("Telegram: отправка", tg.send),
              ("Мост с роботом", lambda: st.check_bridge(hub, paths, s.bridge_key)),
              ("MT5", lambda: st.check_mt5(source, s.symbol, s.mt5_path)),
              ("Claude", lambda: st.check_claude(LLM(s, db))), ("Голос", lambda: st.check_voice_flag(s))]
    if s.voice_enabled:
        stt, tts = build_stt_tts(s)
        checks += [("ffmpeg", st.check_ffmpeg), ("Whisper", lambda: st.check_whisper(stt)),
                   ("Озвучка", lambda: st.check_tts(tts)), ("Звонки (py-tgcalls)", st.check_calls_lib)]
    try:
        return await st.run_selftest(checks)
    finally:
        await tg.close()
        source.shutdown()
        await db.close()


async def run_agent(s: Settings) -> int:
    from agent.analysis.data_mt5 import MT5Source
    from agent.bridge.direct import make_direct_closer
    from agent.storage.db import Database
    from agent.tg.client import Messenger, PyrogramTransport

    for p in s.problems():
        log.warning("настройка: %s", p)
    if not (s.tg_api_id and s.tg_api_hash and s.owner_id):
        log.error("Нужны TG_API_ID, TG_API_HASH, OWNER_ID в .env")
        return 2
    if not Path(str(s.session_path) + ".session").exists():
        # без сессии Pyrogram спросил бы номер телефона в консоли и завис бы в скрытом окне автозапуска
        log.error("Нет сессии Telegram. Запусти start_agent.bat --login (код из Telegram и пароль 2FA)")
        return 3
    db = await Database(s.db_path).open()
    tg = PyrogramTransport(s.tg_api_id, s.tg_api_hash, s.tg_phone, s.owner_id, s.data_dir)
    source = MT5Source(s.mt5_path)
    stt, tts = build_stt_tts(s)
    backend = None
    if s.voice_enabled:                         # звонки нужны только вместе с голосом
        from agent.tg.calls import PyTgCallsBackend
        backend = PyTgCallsBackend(tg.app)
    else:
        log.info("голос выключен (VOICE_ENABLED=false): ассистент работает текстом, звонков нет")
    rt = Runtime(s, db, Messenger(tg), source, tts, stt, backend=backend,
                 direct_close_factory=lambda magic: make_direct_closer(source, int(magic)))

    async def on_msg(client, message) -> None:
        uid = message.from_user.id if message.from_user else None
        uname = message.from_user.username if message.from_user else None
        fwd = tg.is_forwarded(message)
        try:
            if message.voice or message.audio:
                f = Path(s.data_dir) / "voice"
                f.mkdir(parents=True, exist_ok=True)
                if not rt.guard.allowed(uid):
                    await rt.guard.stranger(uid, uname)
                    return
                if rt.stt is None:                        # голос выключен: не качаем файл, просим написать текстом
                    await rt.on_message(uid, uname, voice_path="", forwarded=fwd)
                    return
                path = await tg.download_voice(message, str(f / f"{message.id}.ogg"))
                await rt.on_message(uid, uname, voice_path=path, forwarded=fwd)
            elif message.text:
                await rt.on_message(uid, uname, text=message.text, forwarded=fwd)
        except Exception:
            log.exception("сбой обработки сообщения")

    tg.on_private(on_msg)
    from agent.tg.diagnose import explain_tg_error
    try:
        await tg.start()
    except Exception as e:
        log.error("Не удалось войти в Telegram: %s", explain_tg_error(e))
        await db.close()
        return 3
    if backend is not None:
        await backend.start()
    await rt.start()
    if not await tg.owner_known():
        log.warning("Аккаунт ассистента ещё не знает владельца: напиши ему любое сообщение со своего аккаунта. "
                    "Пока ты не написал, ассистент молчит, но слушает.")
    try:
        await rt.m.text("▶️ Консильери на связи.")
    except Exception as e:                      # не писать первым нельзя, но слушать это не мешает
        log.warning("Приветствие не отправлено: %s", explain_tg_error(e))
    log.info("ассистент запущен")
    try:
        await asyncio.Event().wait()
    finally:
        await rt.stop()
        await tg.stop()
        await db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent.main", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-send", action="store_true", help="в самопроверке не слать тестовое сообщение в Telegram")
    ap.add_argument("--console", action="store_true", help="диалог в терминале без Telegram")
    ap.add_argument("--fake-robot", action="store_true", help="в режиме консоли поднять симулятор робота")
    ap.add_argument("--real-mt5", action="store_true", help="в режиме консоли брать цены из живого MT5")
    a = ap.parse_args(argv)
    s = get_settings()
    setup_logging(s.data_dir)
    try:
        if a.login:
            return asyncio.run(run_login(s))
        if a.selftest:
            return asyncio.run(run_selftest(s, send_test=not a.no_send))
        if a.console:
            return asyncio.run(run_console(s, a.fake_robot, a.real_mt5))
        return asyncio.run(run_agent(s))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
