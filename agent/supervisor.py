"""Присмотр за ассистентом для автозапуска: start_agent.bat --supervised (его запускает start_agent_hidden.vbs).

Политика перезапуска:
 - код выхода 2 (нет настроек в .env), 3 (нет сессии Telegram) или 4 (нет .venv): перезапуск ничего не исправит,
   поэтому присмотрщик останавливается, пишет причину в data/assistant.log и показывает окно с сообщением;
 - любой другой выход (сбой, 0, неудачный запуск): перезапуск с нарастающей паузой 10 с → 30 с → 60 с → 5 минут;
   если ассистент проработал 10 минут и больше, счёт неудач начинается заново.

В лог пишется открытием файла на каждую строку (дописать и закрыть): ротацию журнала у работающего ассистента
(RotatingFileHandler) на Windows не должен блокировать открытый файл присмотрщика.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from agent import exitcodes

ROOT = Path(__file__).resolve().parent.parent
DELAYS = (10, 30, 60, 300)          # секунды: 1-я, 2-я, 3-я подряд неудача, дальше всегда 5 минут
STABLE_SEC = 600                    # столько проработал без сбоя: считаем запуск удачным
TITLE = "Консильери"


def restart_delay(failures: int) -> int:
    """Пауза перед перезапуском после failures-й подряд неудачи."""
    return DELAYS[min(max(failures, 1), len(DELAYS)) - 1]


class FileLog:
    """Строки в том же виде, что у logging: «2026-11-03 15:47:20,123 WARNING supervisor: текст»."""

    def __init__(self, path: Path, now: Callable[[], datetime] = datetime.now):
        self.path, self.now = Path(path), now

    def write(self, level: str, msg: str) -> None:
        stamp = self.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {level} supervisor: {msg}\n")
        except OSError as e:                                     # лог недоступен: хотя бы в stderr
            print(f"{stamp} {level} supervisor: {msg} (лог не записан: {e})", file=sys.stderr)


def show_message(title: str, text: str) -> None:
    """Окно с сообщением (Windows). Блокирует, пока его не закроют. Вне Windows просто печатает."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 | 0x40000)       # значок ошибки, поверх всех окон
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


class Supervisor:
    def __init__(self, run_agent: Callable[[], int], log: FileLog, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, notify: Callable[[str, str], None] = show_message):
        self.run_agent, self.log, self.sleep, self.clock, self.notify = run_agent, log, sleep, clock, notify

    def run(self) -> int:
        """Крутится, пока не получит код, при котором перезапуск бессмыслен. Возвращает этот код."""
        self.log.write("INFO", "присмотрщик запущен")
        failures = 0
        while True:
            started = self.clock()
            try:
                code = self.run_agent()
            except Exception as e:                               # не удалось даже запустить процесс
                self.log.write("ERROR", f"не удалось запустить ассистента: {type(e).__name__}: {e}")
                code = 1
            ran = self.clock() - started
            if code in exitcodes.FATAL:
                reason = exitcodes.FATAL[code]
                self.log.write("ERROR", f"ассистент вышел с кодом {code}: {reason} Автоперезапуск остановлен.")
                self.notify(TITLE, f"Автозапуск остановлен (код {code}).\n\n{reason}\n\n"
                                   f"Подробности: data\\assistant.log. После исправления запусти: schtasks /Run /TN Consigliere")
                return code
            if ran >= STABLE_SEC:
                failures = 0
            failures += 1
            delay = restart_delay(failures)
            self.log.write("WARNING", f"ассистент завершился с кодом {code} через {int(ran)} с; "
                                      f"перезапуск через {delay} с (подряд коротких запусков: {failures})")
            self.sleep(delay)


def _data_dir() -> Path:
    try:
        from agent.config import get_settings
        return Path(get_settings().data_dir)
    except Exception:                                            # битый .env: пишем в папку по умолчанию
        return ROOT / "data"


def main() -> int:
    log = FileLog(_data_dir() / "assistant.log")

    def run_agent() -> int:
        return subprocess.call([sys.executable, "-m", "agent.main"], cwd=str(ROOT))

    try:
        return Supervisor(run_agent, log).run()
    except KeyboardInterrupt:
        log.write("INFO", "присмотрщик остановлен вручную")
        return exitcodes.OK


if __name__ == "__main__":
    sys.exit(main())
