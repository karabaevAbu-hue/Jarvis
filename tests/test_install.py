from pathlib import Path

import re

from tools import install_helper as ih
from tools.install_helper import ensure_bridge_key, ensure_env

ROOT = Path(__file__).resolve().parent.parent


def test_bridge_key_created_once_and_kept(tmp_path):
    (tmp_path / ".env.example").write_text("OWNER_ID=\nBRIDGE_KEY=\nMT5_PATH=C:\\x\n", encoding="utf-8")
    env = ensure_env(tmp_path)
    key, created = ensure_bridge_key(env)
    assert created and len(key) >= 24 and f"BRIDGE_KEY={key}" in env.read_text(encoding="utf-8")
    assert "MT5_PATH=C:\\x" in env.read_text(encoding="utf-8")         # остальное не затронуто
    key2, created2 = ensure_bridge_key(env)
    assert key2 == key and not created2


def test_env_example_has_every_setting_from_the_tz():
    ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    for k in ("TG_API_ID", "TG_API_HASH", "TG_PHONE", "OWNER_ID", "ANTHROPIC_API_KEY", "MODEL_FAST", "MODEL_SMART",
              "DAILY_BUDGET_USD", "BRIDGE_KEY", "BRIDGE_ROOT", "MT5_PATH", "MT5_AUTORESTART", "SYMBOL", "TZ",
              "QUIET_HOURS", "VOICE", "VOICE_TRADES", "STT_MODEL", "STT_DEVICE", "CALL_ON_CRITICAL", "CALL_MAX_MIN",
              "MORNING_HM", "REPORT_HM", "EVENING_ZONES_HM", "WEEKLY", "MSG_LEVEL", "VOICE_ENABLED"):
        assert f"{k}=" in ex, k


def test_env_example_parses_into_settings(tmp_path):
    from agent.config import Settings
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.model_fast == "claude-haiku-5-5" and s.daily_budget_usd == 1.0 and s.quiet_range()[1].hour == 8
    assert s.owner_id == 0 and s.problems()                                # пустой .env сообщает, что заполнить


# ---------------------------------------------------------------- зависимости и голос по флагу
def _reqs(name: str) -> str:
    """Только строки зависимостей, без комментариев: слово «torch» в пояснении пакетом не считается."""
    lines = [l.split("#", 1)[0].strip() for l in (ROOT / name).read_text(encoding="utf-8").splitlines()]
    return "\n".join(l for l in lines if l)


def test_requirements_cover_fresh_install_and_voice_is_separate():
    base, win, voice = _reqs("requirements.txt"), _reqs("requirements-windows.txt"), _reqs("requirements-voice.txt")
    assert re.search(r"^six>=", base, re.M)                                 # mplfinance без six падает (python-dateutil 4.x)
    for heavy in ("faster-whisper", "silero-vad", "torch", "tgcalls"):
        assert heavy not in win, f"{heavy} не должен ставиться без голоса"
    assert "MetaTrader5" in win and "pyrofork" in win and "-r requirements.txt" in win
    # у оригинального tgcrypto нет колёс под Windows/Python 3.12: pip собирал бы его из исходников и падал без C++ (проверено по PyPI)
    assert re.search(r"^tgcrypto-pyrofork>=", win, re.M) and not re.search(r"^tgcrypto\s*[<>=]", win, re.M)
    assert re.search(r"^py-tgcalls>=3\.0,<4", voice, re.M)                  # код звонков написан под 3.x
    assert "faster-whisper" in voice and "silero-vad" in voice and "-r requirements-windows.txt" in voice


def test_voice_is_off_by_default_and_whisper_is_base():
    from agent.config import Settings
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.voice_enabled is False and s.stt_model == "base"
    assert Settings(_env_file=None).voice_enabled is False and Settings(_env_file=None).stt_model == "base"


def test_read_env_value_and_voice_flag(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# x\nVOICE_ENABLED = True   # включить\nSTT_MODEL=\"base\"\n", encoding="utf-8-sig")
    assert ih.voice_enabled(env) is True and ih.read_env_value(env, "STT_MODEL") == "base"
    env.write_text("VOICE_ENABLED=false\n", encoding="utf-8")
    assert ih.voice_enabled(env) is False
    env.write_text("VOICE_ENABLED=\n", encoding="utf-8")
    assert ih.voice_enabled(env) is False and ih.voice_enabled(tmp_path / "нет.env") is False


def test_readonly_env_never_creates_a_file(tmp_path):
    (tmp_path / ".env.example").write_text("VOICE_ENABLED=false\n", encoding="utf-8")
    assert ih.readonly_env(tmp_path) == tmp_path / ".env.example" and not (tmp_path / ".env").exists()
    (tmp_path / ".env").write_text("VOICE_ENABLED=true\n", encoding="utf-8")
    assert ih.readonly_env(tmp_path) == tmp_path / ".env"


def test_check_environment_names_each_problem():
    ok = ih.check_environment(False, version=(3, 12), bits=64, which=lambda n: "C:\\ffmpeg.exe")
    assert [lvl for lvl, _ in ok] == ["OK", "OK"]
    assert "FAIL" in [lvl for lvl, _ in ih.check_environment(False, version=(3, 11), bits=64, which=lambda n: "x")]
    assert any("3.12" in m and "PATH" in m for _, m in ih.check_environment(False, version=(3, 10), bits=64, which=lambda n: "x"))
    assert any(lvl == "FAIL" and "32-битный" in m for lvl, m in ih.check_environment(False, version=(3, 12), bits=32, which=lambda n: "x"))
    assert any(lvl == "WARN" and "3.12" in m for lvl, m in ih.check_environment(False, version=(3, 14), bits=64, which=lambda n: "x"))


def test_missing_ffmpeg_is_warning_without_voice_and_failure_with_voice():
    no_ff = lambda n: None  # noqa: E731
    off = ih.check_environment(False, version=(3, 12), bits=64, which=no_ff)
    assert ("WARN", off[1][1]) == off[1] and "DEPLOY_SERVER.md" in off[1][1] and "winget" not in off[1][1]
    on = ih.check_environment(True, version=(3, 12), bits=64, which=no_ff)
    assert on[1][0] == "FAIL" and "VOICE_ENABLED" in on[1][1]


def test_run_check_exit_code_and_output(tmp_path):
    env = tmp_path / ".env"
    env.write_text("VOICE_ENABLED=true\n", encoding="utf-8")
    lines: list[str] = []
    assert ih.run_check(env, out=lines.append, version=(3, 12), bits=64, which=lambda n: None) == 1
    assert any(l.startswith("[FAIL] ffmpeg") for l in lines)
    env.write_text("VOICE_ENABLED=false\n", encoding="utf-8")
    assert ih.run_check(env, out=lambda s: None, version=(3, 12), bits=64, which=lambda n: None) == 0


def _bat_lines(name: str) -> list[str]:
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_install_bat_checks_python_ffmpeg_and_never_exits_silently():
    text = (ROOT / "install.bat").read_text(encoding="utf-8")
    assert "tools.install_helper --check" in text and "where python" in text and "requirements-voice.txt" in text
    assert "--voice-enabled" in text and "py-tgcalls" not in text.split("requirements-windows.txt")[0]
    # каждый выход с ошибкой идёт через :fail, а он показывает сообщение и держит окно (pause)
    assert re.search(r"^:fail\n(?:.*\n)*?.*call :hold", text, re.M) and "pause" in text
    for ln in _bat_lines("install.bat"):
        assert "exit /b 1" not in ln or ln.strip() == "exit /b 1"


def test_bat_files_have_no_dangling_goto_and_no_paren_inside_blocks():
    """Типичные поломки .bat, которые на Linux иначе не поймать: goto на несуществующую метку и «)» внутри ( ... )."""
    for name in ("install.bat", "install_autostart.bat", "start_agent.bat"):
        lines = _bat_lines(name)
        labels = {l.strip()[1:].lower() for l in lines if l.startswith(":") and not l.startswith("::")}
        for l in lines:
            for m in re.finditer(r"goto\s+:(\w+)", l, re.I):
                assert m.group(1).lower() in labels, f"{name}: goto на неизвестную метку {m.group(1)}"
        depth = 0
        for i, l in enumerate(lines, 1):
            s = l.strip()
            if depth and s != ")" and not s.startswith("rem") and re.search(r"(?<!\^)\)", s):
                raise AssertionError(f"{name}:{i}: «)» внутри блока ( ... ) ломает .bat: {s}")
            if s.endswith("("):
                depth += 1
            elif s == ")" and depth:
                depth -= 1
        assert depth == 0, f"{name}: незакрытая скобка блока"


# ---------------------------------------------------------------- docs/DEPLOY_SERVER.md не расходится с кодом
def test_deploy_guide_follows_the_requested_order_and_names_real_things():
    doc = (ROOT / "docs" / "DEPLOY_SERVER.md").read_text(encoding="utf-8")
    order = ["Автовход", "Сон «никогда»", "MetaTrader 5", "Python 3.12", "ffmpeg", "install.bat", ".env", "F7", "InpBridgeKey",
             "start_agent.bat --login", "start_agent.bat --selftest", "install_autostart.bat"]
    pos = []
    for token in order:
        i = doc.find(token, pos[-1] if pos else 0)
        assert i >= 0, f"в инструкции нет шага «{token}» после предыдущего"
        pos.append(i)
    assert pos == sorted(pos)
    # RDP: закрывать крестиком, не «Выйти»
    assert "крестиком" in doc and "Выйти" in doc and "Sign out" in doc
    # где взять ключи
    for token in ("my.telegram.org", "@userinfobot", "console.anthropic.com", "TG_API_ID", "TG_API_HASH", "OWNER_ID",
                  "ANTHROPIC_API_KEY", "shell:startup", "powercfg", "InpEquityStopUSD", "GOLD#"):
        assert token in doc, token
    # каждый .bat из инструкции существует, каждый ключ .env есть в образце
    for bat in set(re.findall(r"\b([\w]+\.bat)\b", doc)):
        assert (ROOT / bat).exists(), bat
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in set(re.findall(r"`([A-Z][A-Z_0-9]{3,})`", doc)):
        if key in ("GOLD", "SCHEME_MIN"):
            continue
        if key.startswith(("TG_", "OWNER", "ANTHROPIC", "BRIDGE", "VOICE", "STT", "SYMBOL", "MT5", "DAILY")):
            assert f"{key}=" in example, f"{key} из инструкции нет в .env.example"
    assert (ROOT / "requirements-voice.txt").exists() and "--no-send" in doc


# ---------------------------------------------------------------- повторный install.bat: проверка версии Python внутри .venv
def _fake_run(stdout="", code=0):
    class R:
        returncode, stderr = code, ""
    R.stdout = stdout
    return lambda *a, **k: R()


def _venv_with_python(root: Path) -> Path:
    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("заглушка", encoding="utf-8")
    return py


def test_check_venv_missing_is_fine_and_nothing_to_recreate(tmp_path):
    assert ih.check_venv(tmp_path)[0] == "missing" and ih.run_check_venv(tmp_path, out=lambda s: None) == 0


def test_check_venv_accepts_3_12_and_newer_and_recreates_older_or_broken(tmp_path):
    _venv_with_python(tmp_path)
    assert ih.check_venv(tmp_path, run=_fake_run("3 12\n"))[0] == "ok"
    assert ih.check_venv(tmp_path, run=_fake_run("3 13\n"))[0] == "ok"
    state, msg = ih.check_venv(tmp_path, run=_fake_run("3 11\n"))
    assert state == "old" and "3.11" in msg and "3.12" in msg
    assert ih.check_venv(tmp_path, run=_fake_run("3 9\n"))[0] == "old"
    assert ih.check_venv(tmp_path, run=_fake_run("", code=1))[0] == "broken"            # python внутри падает
    assert ih.check_venv(tmp_path, run=_fake_run("мусор"))[0] == "broken"
    codes = [ih.run_check_venv(tmp_path, out=lambda s: None, run=_fake_run(v)) for v in ("3 12\n", "3 11\n", "")]
    assert codes == [0, 1, 1]                                                           # код 1 = install.bat пересоздаёт .venv


def test_check_venv_on_a_real_venv_and_on_an_unrunnable_python(tmp_path):
    import subprocess as sp
    import sys
    sp.run([sys.executable, "-m", "venv", "--without-pip", str(tmp_path / ".venv")], check=True, capture_output=True)
    state, msg = ih.check_venv(tmp_path)
    assert state == "ok" and msg.startswith(".venv на Python 3.")                       # настоящий python внутри venv
    py = ih.venv_python(tmp_path)
    assert py is not None
    py.unlink()
    py.write_text("не программа", encoding="utf-8")                                     # файл есть, запустить нельзя
    state, msg = ih.check_venv(tmp_path)
    assert state == "broken" and "не запускается" in msg


def test_install_bat_recreates_an_old_venv_and_explains_a_locked_one():
    bat = (ROOT / "install.bat").read_text(encoding="utf-8")
    assert bat.index("--check-venv") < bat.index("python -m venv .venv")                # проверка до создания
    assert re.search(r"--check-venv\s*\nif errorlevel 1 goto :drop_venv\ngoto :venv_make", bat)
    drop = bat[bat.index("\n:drop_venv\n"):bat.index("\n:venv_make\n")]               # метки в начале строки, не «goto :...»
    assert 'rmdir /s /q ".venv"' in drop and 'if exist ".venv" goto :venv_locked' in drop
    locked = bat[bat.index("\n:venv_locked\n"):bat.index("\n:no_venv\n")]
    assert "schtasks /End /TN Consigliere" in locked and "goto :fail" in locked          # занятое окружение: понятный выход, не молчание


# ---------------------------------------------------------------- предупреждения в документах
BOLD_EQUITY = "**`InpEquityStopUSD` по умолчанию 0 = защита выключена. Для счёта $100 ставь, например, 60.**"


def test_equity_stop_default_is_a_bold_warning_in_both_docs():
    for name in ("mt5/README.md", "docs/DEPLOY_SERVER.md"):
        doc = (ROOT / name).read_text(encoding="utf-8")
        assert BOLD_EQUITY in doc, name
    robot = (ROOT / "mt5" / "MrCasino.mq5").read_text(encoding="utf-8")
    assert re.search(r"^input\s+double\s+InpEquityStopUSD\s*=\s*0\.0;", robot, re.M)              # документ не врёт про умолчание


def test_deploy_guide_describes_the_restart_policy_of_the_autostart():
    from agent.supervisor import DELAYS
    doc = (ROOT / "docs" / "DEPLOY_SERVER.md").read_text(encoding="utf-8")
    assert DELAYS == (10, 30, 60, 300)
    assert "10 секунд, 30 секунд, 60 секунд, потом каждые 5 минут" in doc
    assert "коде выхода 2" in doc and "или 3 (нет сессии Telegram)" in doc and "schtasks /Run /TN Consigliere" in doc
    assert "schtasks /End /TN Consigliere" in doc and "assistant.log" in doc
    mt5 = (ROOT / "mt5" / "README.md").read_text(encoding="utf-8").lower()
    assert "на 2% выше порога" in mt5 and "inpminslpt" in mt5 and "не чаще раза в минуту" in mt5


def test_requirements_dev_holds_pytest_and_the_server_does_not_get_it():
    dev = _reqs("requirements-dev.txt")
    assert "-r requirements.txt" in dev and re.search(r"^pytest>=", dev, re.M) and re.search(r"^pytest-asyncio>=", dev, re.M)
    for name in ("requirements.txt", "requirements-windows.txt", "requirements-voice.txt"):
        assert "pytest" not in _reqs(name), f"{name}: pytest не нужен на сервере"
    wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements-dev.txt" in wf and "pip install -r requirements.txt" not in wf
    assert "requirements-dev.txt" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pytest" not in (ROOT / "install.bat").read_text(encoding="utf-8")
