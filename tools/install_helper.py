"""Помощник install.bat: проверяет окружение, создаёт BRIDGE_KEY в .env, качает модель Whisper (если голос включён).

    python -m tools.install_helper                  # .env из .env.example + ключ моста (если пуст)
    python -m tools.install_helper --check          # Python 3.12+, 64 бита, ffmpeg; код 1, если без этого нельзя
    python -m tools.install_helper --voice-enabled  # код 0, если в .env VOICE_ENABLED=true, иначе 1
    python -m tools.install_helper --models         # модель Whisper из STT_MODEL (нужен requirements-voice.txt)

Работает на одной стандартной библиотеке: install.bat вызывает его системным Python до создания venv.
"""
from __future__ import annotations

import re
import secrets
import shutil
import struct
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


def ensure_env(root: Path = ROOT) -> Path:
    env, ex = root / ".env", root / ".env.example"
    if not env.exists():
        shutil.copyfile(ex, env)
    return env


def ensure_bridge_key(env_path: Path) -> tuple[str, bool]:
    """Возвращает (ключ, создан ли сейчас). Существующий ключ не трогает."""
    text = env_path.read_text(encoding="utf-8")
    m = re.search(r"^BRIDGE_KEY=(.*)$", text, re.M)
    if m and m.group(1).strip():
        return m.group(1).strip(), False
    key = secrets.token_urlsafe(24)
    if m:
        text = text[:m.start()] + f"BRIDGE_KEY={key}" + text[m.end():]
    else:
        text += f"\nBRIDGE_KEY={key}\n"
    env_path.write_text(text, encoding="utf-8")
    return key, True


def read_env_value(env_path: Path, key: str, default: str = "") -> str:
    """Значение KEY из .env без зависимостей. Пустая строка и отсутствие файла дают default."""
    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except OSError:
        return default
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", text, re.M)
    val = (m.group(1) if m else "").split("#", 1)[0].strip().strip("\"'")
    return val or default


def voice_enabled(env_path: Path) -> bool:
    return read_env_value(env_path, "VOICE_ENABLED", "false").lower() in ("1", "true", "yes", "on", "да")


def check_environment(voice: bool, version: tuple[int, int] | None = None, bits: int | None = None,
                      which: Callable[[str], str | None] = shutil.which) -> list[tuple[str, str]]:
    """Список (уровень, сообщение): FAIL останавливает установку, WARN только предупреждает, OK для порядка."""
    ver = version or sys.version_info[:2]
    bits = bits or struct.calcsize("P") * 8
    out: list[tuple[str, str]] = []
    if ver < (3, 12):
        out.append(("FAIL", f"Python {ver[0]}.{ver[1]} слишком старый, нужен 3.12. Скачай с python.org/downloads "
                            f"и при установке отметь галочку «Add python.exe to PATH»."))
    elif ver > (3, 13):
        out.append(("WARN", f"Python {ver[0]}.{ver[1]}: проверено на 3.12 и 3.13. Если pip не поставит MetaTrader5, "
                            f"поставь Python 3.12."))
    else:
        out.append(("OK", f"Python {ver[0]}.{ver[1]} подходит"))
    if bits != 64:
        out.append(("FAIL", "Python 32-битный: пакет MetaTrader5 работает только с 64-битным. Поставь Python 3.12 (64-bit)."))
    ff = which("ffmpeg")
    if ff:
        out.append(("OK", f"ffmpeg найден: {ff}"))
    elif voice:
        out.append(("FAIL", "ffmpeg не найден, а в .env включён голос (VOICE_ENABLED=true). "
                            "Поставь ffmpeg по docs\\DEPLOY_SERVER.md (шаг 5), закрой и открой окно командной строки, запусти install.bat снова."))
    else:
        out.append(("WARN", "ffmpeg не найден. Сейчас он не нужен (голос выключен). Понадобится, если включишь "
                            "VOICE_ENABLED=true (docs\\DEPLOY_SERVER.md, шаг 5)."))
    return out


def readonly_env(root: Path = ROOT) -> Path:
    """Файл для чтения настроек без побочных эффектов: .env, а если его ещё нет, образец .env.example."""
    env = root / ".env"
    return env if env.exists() else root / ".env.example"


def run_check(env_path: Path, out=print, **kw) -> int:
    results = check_environment(voice_enabled(env_path), **kw)
    for level, msg in results:
        out(f"[{level}] {msg}")
    return 1 if any(level == "FAIL" for level, _ in results) else 0


def download_models() -> None:
    from agent.config import get_settings
    from faster_whisper import WhisperModel
    s = get_settings()
    print(f"Скачиваю модель Whisper «{s.stt_model}» (до 2 ГБ места)...")
    WhisperModel(s.stt_model, device="cpu", compute_type="int8", download_root=str(Path(s.data_dir) / "models"))
    print("Модель готова.")


def main() -> int:
    if "--check" in sys.argv:
        return run_check(readonly_env())
    if "--voice-enabled" in sys.argv:
        return 0 if voice_enabled(readonly_env()) else 1
    if "--models" in sys.argv:
        download_models()
        return 0
    env = ensure_env()
    key, created = ensure_bridge_key(env)
    print(f"Файл настроек: {env}")
    print(f"BRIDGE_KEY {'создан' if created else 'уже был'}: {key}")
    print("Вставь этот ключ в параметр InpBridgeKey робота MT5 (должен совпадать).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
