"""Помощник install.bat: создаёт BRIDGE_KEY в .env и скачивает модель Whisper.

    python -m tools.install_helper            # ключ моста (если пуст) + .env из .env.example
    python -m tools.install_helper --models   # модель Whisper из STT_MODEL
"""
from __future__ import annotations

import re
import secrets
import shutil
import sys
from pathlib import Path

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


def download_models() -> None:
    from agent.config import get_settings
    from faster_whisper import WhisperModel
    s = get_settings()
    print(f"Скачиваю модель Whisper «{s.stt_model}» (до 2 ГБ места)...")
    WhisperModel(s.stt_model, device="cpu", compute_type="int8", download_root=str(Path(s.data_dir) / "models"))
    print("Модель готова.")


def main() -> int:
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
