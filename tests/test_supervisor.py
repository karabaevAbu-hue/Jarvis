"""Автозапуск: присмотрщик (agent/supervisor.py), коды выхода и скрипты start_agent*.bat / .vbs."""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from agent import exitcodes
from agent.supervisor import DELAYS, STABLE_SEC, TITLE, FileLog, Supervisor, restart_delay

ROOT = Path(__file__).resolve().parent.parent


class Done(BaseException):
    """Конец сценария теста. Наследует BaseException: присмотрщик перехватывает Exception и крутился бы дальше."""


class Rig:
    """Подставные запуск, пауза, часы и окно: сценарий — список (код выхода или исключение, сколько проработал, с)."""

    def __init__(self, tmp_path, script):
        self.script, self.sleeps, self.notes, self.now = list(script), [], [], 0.0
        self.log_path = tmp_path / "data" / "assistant.log"
        self.sup = Supervisor(self.run_agent, FileLog(self.log_path, now=lambda: datetime(2026, 11, 3, 15, 47, 20, 123000)),
                              sleep=self.sleeps.append, clock=lambda: self.now,
                              notify=lambda title, text: self.notes.append((title, text)))

    def run_agent(self):
        if not self.script:
            raise Done()
        outcome, ran = self.script.pop(0)
        self.now += ran
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def go(self):
        try:
            return self.sup.run()
        except Done:
            return None

    @property
    def log(self):
        return self.log_path.read_text(encoding="utf-8")


def test_pause_grows_10_30_60_then_five_minutes():
    assert DELAYS == (10, 30, 60, 300)
    assert [restart_delay(n) for n in (1, 2, 3, 4, 5, 50)] == [10, 30, 60, 300, 300, 300]
    assert restart_delay(0) == 10                                          # на всякий случай не падаем


def test_crashes_back_off_and_every_restart_is_logged(tmp_path):
    rig = Rig(tmp_path, [(1, 3)] * 6)
    assert rig.go() is None
    assert rig.sleeps == [10, 30, 60, 300, 300, 300] and rig.notes == []
    assert rig.log.count("перезапуск через") == 6
    assert "перезапуск через 300 с (подряд коротких запусков: 4)" in rig.log


def test_a_long_healthy_run_starts_the_count_again(tmp_path):
    rig = Rig(tmp_path, [(1, 5), (1, 5), (1, STABLE_SEC + 1), (1, 5)])
    rig.go()
    assert rig.sleeps == [10, 30, 10, 30]                                  # после стабильных 10 минут снова с 10 с
    rig2 = Rig(tmp_path / "b", [(1, 5), (1, STABLE_SEC - 1), (1, 5)])
    rig2.go()
    assert rig2.sleeps == [10, 30, 60]                                     # почти 10 минут стабильностью не считаются


@pytest.mark.parametrize("code,needle", [(2, ".env"), (3, "--login"), (4, "install.bat")])
def test_fatal_codes_stop_at_once_log_the_reason_and_show_a_message(tmp_path, code, needle):
    rig = Rig(tmp_path, [(code, 2), (1, 2)])
    assert rig.go() == code
    assert rig.sleeps == [] and rig.script == [(1, 2)]                     # ни паузы, ни повторного запуска
    assert f"ERROR supervisor: ассистент вышел с кодом {code}" in rig.log and "Автоперезапуск остановлен" in rig.log
    assert len(rig.notes) == 1
    title, text = rig.notes[0]
    assert title == TITLE and f"код {code}" in text and needle in text and "assistant.log" in text and "schtasks /Run" in text


def test_fatal_code_after_some_crashes_still_stops_without_extra_sleep(tmp_path):
    rig = Rig(tmp_path, [(1, 3), (1, 3), (exitcodes.NO_SESSION, 3), (1, 3)])
    assert rig.go() == exitcodes.NO_SESSION
    assert rig.sleeps == [10, 30]


def test_failed_launch_is_logged_and_retried_with_backoff_and_zero_exit_also_restarts(tmp_path):
    rig = Rig(tmp_path, [(OSError("нет такого файла"), 0), (0, 1), (1, 1)])
    rig.go()
    assert "не удалось запустить ассистента: OSError: нет такого файла" in rig.log
    assert rig.sleeps == [10, 30, 60]                                      # неожиданный выход с 0 без оператора: тоже перезапуск


def test_log_line_format_matches_logging_and_appends(tmp_path):
    p = tmp_path / "d" / "assistant.log"
    log = FileLog(p, now=lambda: datetime(2026, 11, 3, 15, 47, 20, 123000))
    log.write("WARNING", "раз")
    log.write("ERROR", "два")
    assert p.read_text(encoding="utf-8").splitlines() == ["2026-11-03 15:47:20,123 WARNING supervisor: раз",
                                                          "2026-11-03 15:47:20,123 ERROR supervisor: два"]


def test_unwritable_log_does_not_kill_the_supervisor(tmp_path, capsys):
    bad = tmp_path / "is_a_dir"
    bad.mkdir()
    FileLog(bad).write("INFO", "x")                                        # путь — папка: запись невозможна
    assert "лог не записан" in capsys.readouterr().err


# ------------------------------------------------ сквозной прогон: настоящий процесс ассистента
def _agent_exit_code(tmp_path, **env):
    e = {**os.environ, "DATA_DIR": str(tmp_path), "ANTHROPIC_API_KEY": "", "BRIDGE_KEY": "k", "PYTHONIOENCODING": "utf-8"}
    for k in ("TG_API_ID", "TG_API_HASH", "OWNER_ID"):
        e.pop(k, None)
    e.update(env)
    return subprocess.call([sys.executable, "-m", "agent.main"], cwd=ROOT, env=e, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)


def test_real_assistant_exits_with_2_without_settings_and_with_3_without_a_session(tmp_path):
    assert _agent_exit_code(tmp_path) == exitcodes.NO_SETTINGS
    assert _agent_exit_code(tmp_path, TG_API_ID="12345", TG_API_HASH="abc", OWNER_ID="5") == exitcodes.NO_SESSION


def test_real_assistant_with_a_broken_env_value_exits_with_2_not_a_traceback(tmp_path):
    assert _agent_exit_code(tmp_path, TG_API_ID="abc") == exitcodes.NO_SETTINGS


def test_supervisor_stops_on_the_real_assistant_missing_a_session(tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path), "TG_API_ID": "12345", "TG_API_HASH": "abc", "OWNER_ID": "5",
           "BRIDGE_KEY": "k", "ANTHROPIC_API_KEY": "", "PYTHONIOENCODING": "utf-8"}
    notes = []
    sup = Supervisor(lambda: subprocess.call([sys.executable, "-m", "agent.main"], cwd=ROOT, env=env,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120),
                     FileLog(tmp_path / "assistant.log"), sleep=lambda s: pytest.fail("не должен спать"),
                     notify=lambda t, x: notes.append(x))
    assert sup.run() == exitcodes.NO_SESSION
    assert "--login" in notes[0] and "вышел с кодом 3" in (tmp_path / "assistant.log").read_text(encoding="utf-8")


# ------------------------------------------------ скрипты
def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_start_agent_bat_has_a_supervised_mode_and_signals_a_missing_venv_with_4():
    bat = _read("start_agent.bat")
    assert re.search(r'if /i "%~1"=="--supervised" goto :supervised', bat) and "python -m agent.supervisor" in bat
    assert f"exit /b {exitcodes.NOT_INSTALLED}" in bat and "python -m agent.main %*" in bat
    assert bat.count("exit /b %errorlevel%") == 2 and "pause" not in re.sub(r"(?im)^rem .*$", "", bat)   # пауза зависила бы автозапуск


def test_hidden_launcher_is_a_thin_one_shot_without_its_own_restart_loop():
    vbs = _read("start_agent_hidden.vbs")
    code = "\n".join(l for l in vbs.splitlines() if not l.lstrip().startswith("'"))
    assert "start_agent.bat --supervised" in code
    assert not re.search(r"(?im)^\s*(Do|Loop|While|Wend)\b", code)           # вечный цикл ушёл в присмотрщик
    handled = {int(x) for x in re.findall(r"rc <> (\d+)", code)} - {0}
    assert handled == {exitcodes.NO_SETTINGS, exitcodes.NO_SESSION}          # эти два случая присмотрщик показал сам
    for line in code.splitlines():
        if "MsgBox" in line:
            assert line.isascii(), "wscript читает файл в ANSI: русский текст в MsgBox превратится в кракозябры"
    assert 'exitcodes' not in vbs and f"WScript.Quit {exitcodes.NOT_INSTALLED}" in code


def test_autostart_installer_needs_the_venv_and_still_registers_the_hidden_launcher():
    bat = _read("install_autostart.bat")
    assert bat.index(".venv\\Scripts\\python.exe") < bat.index("schtasks /Create")
    assert 'schtasks /Create /F /TN "Consigliere"' in bat and "start_agent_hidden.vbs" in bat and "/SC ONLOGON" in bat
    assert "10 с, 30 с, 60 с" in bat and "schtasks /End /TN Consigliere" in bat and "schtasks /Run /TN Consigliere" in bat


def test_every_fatal_code_has_a_reason_that_says_what_to_do():
    assert set(exitcodes.FATAL) == {2, 3, 4}
    assert ".env" in exitcodes.FATAL[2] and "--login" in exitcodes.FATAL[3] and "install.bat" in exitcodes.FATAL[4]
