"""start_agent.bat --selftest: проверка MT5, моста, Claude, Whisper, озвучки и диска. Итог по-русски."""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.config import Settings

Result = tuple[bool, str]


def check_config(s: Settings) -> Result:
    p = s.problems()
    return (not p, "настройки .env заполнены" if not p else "в .env не хватает: " + "; ".join(p))


def check_ffmpeg() -> Result:
    p = shutil.which("ffmpeg")
    return (bool(p), f"ffmpeg найден: {p}" if p else "ffmpeg не найден: winget install Gyan.FFmpeg")


def check_disk(s: Settings, min_gb: float = 1.0) -> Result:
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(s.data_dir).free / 1e9
    return (free >= min_gb, f"на диске свободно {free:.1f} ГБ" + ("" if free >= min_gb else ": мало, нужен минимум 1 ГБ"))


def check_mt5(source: Any, symbol: str = "") -> Result:
    try:
        sym = source.find_symbol(symbol)
        if not sym:
            return False, "MT5 запущен, но символ золота не найден (XAUUSDm / XAUUSDc / GOLD#): задай SYMBOL в .env"
        df = source.candles(sym, "H1", 50)
        return True, f"MT5 отвечает: {sym}, последняя свеча H1 {df.index[-1]:%d.%m %H:%M} UTC, close {df['close'].iloc[-1]:.2f}"
    except Exception as e:
        return False, f"MT5 не запущен или недоступен: {e}"


async def check_bridge(hub: Any, paths: Any, key: str) -> Result:
    if paths is None:
        return False, "папка моста робота не найдена: робот v3.3 не запускался или другой BRIDGE_ROOT"
    st = hub.status()
    if st is None:
        return False, f"status.json нет в {paths.base}: робот не стоит на графике или InpBridge выключен"
    if st.age_sec > 60:
        return False, f"status.json старый ({int(st.age_sec)} с): MT5 завис или робот снят с графика"
    if not key:
        return False, "BRIDGE_KEY пуст: робот не примет команды"
    hub.write_heartbeat()
    await hub.poll_once()                       # курсор событий ставим до команды, иначе быстрый ответ пропустим
    res = await hub.command("ping", timeout=6.0)
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


def check_telegram_session(s: Settings) -> Result:
    f = Path(str(s.session_path) + ".session")
    return (f.exists(), "сессия Telegram есть" if f.exists() else "сессии Telegram нет: запусти start_agent.bat --login")


async def run_selftest(checks: list[tuple[str, Callable[[], Awaitable[Result] | Result]]]) -> int:
    """Выполняет проверки по порядку, печатает ✔/✖ и итог. Код возврата 0, если всё хорошо."""
    import inspect
    bad = 0
    print("Самопроверка Консильери\n")
    for name, fn in checks:
        try:
            r = fn()
            ok, msg = await r if inspect.isawaitable(r) else r
        except Exception as e:
            ok, msg = False, f"проверка упала: {type(e).__name__}: {e}"
        print(f"{'✔' if ok else '✖'} {name}: {msg}")
        bad += 0 if ok else 1
    print("\nИтог: " + ("всё в порядке, можно запускать." if not bad else f"проблем: {bad}. Исправь их и запусти снова."))
    return 0 if not bad else 1
