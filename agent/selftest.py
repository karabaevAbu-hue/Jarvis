"""start_agent.bat --selftest: проверка настроек, Telegram, моста, MT5, Claude, диска (и голоса, если он включён).

Каждая проверка отвечает отдельно и называет причину по-русски. Результат проверки: (True, текст) — хорошо,
(False, текст) — проблема, (None, текст) — пропущено, потому что сломано то, от чего она зависит.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.config import Settings
from agent.tg.diagnose import explain_tg_error, owner_unknown_text

Result = tuple["bool | None", str]


def check_config(s: Settings) -> Result:
    p = s.problems()
    return (not p, "настройки .env заполнены" if not p else "в .env не хватает: " + "; ".join(p))


def check_ffmpeg() -> Result:
    p = shutil.which("ffmpeg")
    return (bool(p), f"ffmpeg найден: {p}" if p else "ffmpeg не найден (нужен только для голоса): см. docs\\DEPLOY_SERVER.md, шаг 5")


def check_voice_flag(s: Settings) -> Result:
    if s.voice_enabled:
        return True, f"голос включён (VOICE_ENABLED=true): Whisper {s.stt_model}, озвучка {s.voice}, звонки"
    return True, "голос выключен (VOICE_ENABLED=false): ассистент работает текстом, звонков нет, проверки Whisper и озвучки не нужны"


def check_disk(s: Settings, min_gb: float = 1.0) -> Result:
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(s.data_dir).free / 1e9
    return (free >= min_gb, f"на диске свободно {free:.1f} ГБ" + ("" if free >= min_gb else ": мало, нужен минимум 1 ГБ"))


def check_mt5(source: Any, symbol: str = "", path: str = "") -> Result:
    """Доступ к MT5 из Python. Различает: нет терминала, не запущен, нет связи с брокером, выключен алготрейдинг."""
    if path and not Path(path).is_file():
        return False, (f"терминал не найден по MT5_PATH={path}: поправь путь в .env "
                       "(обычно C:\\Program Files\\MetaTrader 5\\terminal64.exe)")
    try:
        sym = source.find_symbol(symbol)
    except Exception as e:
        return False, _mt5_down(e)
    if not sym:
        return False, ("MT5 отвечает, но символ золота не найден (XAUUSDm / XAUUSDc / GOLD#): "
                       "добавь символ в Обзор рынка MT5 или задай SYMBOL в .env")
    state = None
    if hasattr(source, "terminal_state"):
        try:
            state = source.terminal_state()
        except Exception as e:
            return False, _mt5_down(e)
    if state:
        if not state.get("connected", True):
            return False, ("терминал MT5 запущен, но нет связи с брокером: проверь интернет сервера и вход в счёт "
                           "(внизу справа в MT5 должны бежать столбики связи)")
        if not state.get("trade_allowed", True):
            return False, ("в MT5 выключена кнопка «Алготорговля» (Algo Trading): робот не сможет открывать сделки. "
                           "Включи кнопку на панели и в Сервис, Настройки, Советники")
    try:
        df = source.candles(sym, "H1", 50)
    except Exception as e:
        return False, _mt5_down(e)
    acc = ""
    if state and state.get("login"):
        kind = {True: "ДЕМО", False: "РЕАЛЬНЫЙ ⚠️"}.get(state.get("demo"), "тип счёта неизвестен")
        acc = f"; счёт {state['login']} ({kind}) {state.get('server', '')}".rstrip()
    return True, (f"MT5 отвечает: {sym}, последняя свеча H1 {df.index[-1]:%d.%m %H:%M} UTC, "
                  f"close {df['close'].iloc[-1]:.2f}{acc}")


def _mt5_down(e: Exception) -> str:
    msg = str(e)
    if "пакет MetaTrader5" in msg:
        return "Python-пакет MetaTrader5 не установлен или это не Windows: запусти install.bat на сервере с MT5"
    return ("MT5 не запущен, закрыт или не вошёл в счёт: открой терминал, войди в демо-счёт и повтори. "
            f"Технически: {msg}")


async def check_bridge(hub: Any, paths: Any, key: str, ping_timeout: float = 6.0) -> Result:
    if paths is None:
        return False, "папка моста робота не найдена: робот не запускался (нет советника на графике или InpBridge выключен) или другой BRIDGE_ROOT"
    st = hub.status()
    if st is None:
        return False, f"status.json нет в {paths.base}: робот не стоит на графике или InpBridge выключен"
    if st.age_sec > 60:
        return False, f"status.json старый ({int(st.age_sec)} с): MT5 завис или робот снят с графика"
    if not key:
        return False, "BRIDGE_KEY пуст: робот не примет команды"
    hub.write_heartbeat()
    await hub.poll_once()                       # курсор событий ставим до команды, иначе быстрый ответ пропустим
    res = await hub.command("ping", timeout=ping_timeout)
    if not res.ok and getattr(res, "timeout", False):
        return False, (f"status.json свежий, но робот не отвечает на команды за {ping_timeout:g} с: он завис или снят с графика. "
                       "Открой MT5: у робота на графике должна быть улыбка, а в журнале «Эксперты» нет ошибок")
    if not res.ok:
        return False, f"мост читается, но команда ping не прошла: {res.msg}. Проверь, что InpBridgeKey в роботе равен BRIDGE_KEY"
    return True, f"мост работает: робот {paths.robot} v{st.get('version')}, ping прошёл"


async def check_claude(llm: Any) -> Result:
    if not llm.available:
        return False, "ANTHROPIC_API_KEY не задан: ассистент будет отвечать шаблонами"
    try:
        async def none(name, args):
            return {}
        t = time.time()
        r = await llm.run_turn(kind="fast", system_static="Отвечай одним словом.", system_dynamic="",
                               messages=[{"role": "user", "content": "Скажи: ок"}], tools=[], dispatch=none,
                               max_tokens=256)
        return True, f"Claude отвечает ({llm.model_for('fast')}): «{r.text[:30]}», {time.time() - t:.1f} с, ${r.cost:.5f}"
    except Exception as e:
        return False, f"Claude не отвечает: {type(e).__name__}: {e}"


async def check_whisper(stt: Any) -> Result:
    try:
        t = time.time()
        await stt.transcribe_pcm16k(b"\x00\x00" * 16000)
        return True, f"Whisper работает ({stt.model_size}), загрузка и проход {time.time() - t:.1f} с"
    except Exception as e:
        return False, f"Whisper не запускается: {type(e).__name__}: {e}"


async def check_tts(tts: Any) -> Result:
    try:
        audio = await tts.synth("Проверка голоса. Консильери на связи.")
        if audio:
            return True, f"озвучка работает ({tts.voice}), {len(audio) // 1024} КБ"
        return False, "озвучка недоступна (edge-tts и голос Windows): ассистент будет писать текстом"
    except Exception as e:
        return False, f"озвучка упала: {type(e).__name__}: {e}"


def check_calls_lib() -> Result:
    """Библиотека звонков: те же импорты, что делает PyTgCallsBackend (py-tgcalls 3.x)."""
    try:
        import importlib.metadata as md
        from ntgcalls import MediaSource  # noqa: F401
        from pytgcalls import PyTgCalls, filters  # noqa: F401
        from pytgcalls.types import ChatUpdate, Device, Direction  # noqa: F401
        from pytgcalls.types.raw import AudioParameters, AudioStream, Stream  # noqa: F401
        return True, f"py-tgcalls {md.version('py-tgcalls')} подходит для кода звонков (нужна 3.x); живой звонок: tools/call_prototype.py"
    except ImportError as e:
        return False, f"py-tgcalls не установлен или не той версии ({e}): pip install -r requirements-voice.txt"


def check_telegram_session(s: Settings) -> Result:
    f = Path(str(s.session_path) + ".session")
    return (f.exists(), "сессия Telegram есть" if f.exists() else "сессии Telegram нет: запусти start_agent.bat --login")


class TelegramCheck:
    """Три отдельные проверки Telegram: вход, владелец, отправка. Если вход не прошёл, остальные пропускаются.

    probe_factory создаёт объект с async-методами connect() -> bool, me(), owner(owner_id), send(owner_id, text),
    disconnect() (в бою PyrogramProbe, в тестах двойник). Ничего интерактивного: нет сессии — сразу понятная
    подсказка, а не вопрос про номер телефона.
    """

    def __init__(self, s: Settings, probe_factory: Callable[[], Any], send_test: bool = True, timeout: float = 30.0):
        self.s, self.factory, self.send_test, self.timeout = s, probe_factory, send_test, timeout
        self.probe: Any = None
        self.me: dict | None = None

    async def login(self) -> Result:
        s = self.s
        if not (s.tg_api_id and s.tg_api_hash):
            return False, "TG_API_ID / TG_API_HASH не заполнены в .env (my.telegram.org, API development tools)"
        if not Path(str(s.session_path) + ".session").exists():
            return False, "сессии нет: запусти start_agent.bat --login (код из Telegram и пароль 2FA)"
        try:
            self.probe = self.factory()
            if not await asyncio.wait_for(self.probe.connect(), self.timeout):
                await self.close()
                return False, "файл сессии есть, но вход не завершён: удали data\\consigliere.session и выполни start_agent.bat --login"
            self.me = await asyncio.wait_for(self.probe.me(), self.timeout)
        except Exception as e:
            await self.close()
            return False, explain_tg_error(e)
        me = self.me
        who = f"{me['name']} (id {me['id']}{', @' + me['username'] if me.get('username') else ''})"
        if s.owner_id and me["id"] == s.owner_id:
            return False, (f"в сессии твой собственный аккаунт {who}, а нужен отдельный аккаунт ассистента: "
                           "иначе ассистент пишет сам себе. Удали сессию и войди аккаунтом ассистента (раздел 9 ТЗ)")
        return True, f"вошёл как {who}"

    async def owner(self) -> Result:
        if self.probe is None:
            return None, "пропущено: нет входа в Telegram"
        if not self.s.owner_id:
            return False, "OWNER_ID не заполнен в .env (узнать число через @userinfobot)"
        try:
            o = await asyncio.wait_for(self.probe.owner(self.s.owner_id), self.timeout)
        except Exception as e:
            return False, explain_tg_error(e)
        if not o:
            return False, owner_unknown_text()
        return True, f"владелец найден: {o['name']} (id {o['id']})"

    async def send(self) -> Result:
        if self.probe is None:
            return None, "пропущено: нет входа в Telegram"
        if not self.send_test:
            return None, "пропущено по просьбе (--no-send)"
        if not self.s.owner_id:
            return None, "пропущено: нет OWNER_ID"
        try:
            mid = await asyncio.wait_for(self.probe.send(self.s.owner_id,
                                                         "✅ Самопроверка Консильери: Telegram работает."), self.timeout)
        except Exception as e:
            return False, explain_tg_error(e)
        return True, f"тестовое сообщение отправлено (id {mid}): посмотри его в Telegram"

    async def close(self) -> None:
        p, self.probe = self.probe, None
        if p is not None:
            try:
                await p.disconnect()
            except Exception:
                pass


async def run_selftest(checks: list[tuple[str, Callable[[], Awaitable[Result] | Result]]]) -> int:
    """Выполняет проверки по порядку, печатает ✔ / ✖ / – и итог. Код возврата 0, если проблем нет."""
    import inspect
    bad = 0
    print("Самопроверка Консильери\n")
    for name, fn in checks:
        try:
            r = fn()
            ok, msg = await r if inspect.isawaitable(r) else r
        except Exception as e:
            ok, msg = False, f"проверка упала: {type(e).__name__}: {e}"
        print(f"{'✔' if ok else '–' if ok is None else '✖'} {name}: {msg}")
        bad += 1 if ok is False else 0
    print("\nИтог: " + ("всё в порядке, можно запускать." if not bad else f"проблем: {bad}. Исправь их и запусти снова."))
    return 0 if not bad else 1
