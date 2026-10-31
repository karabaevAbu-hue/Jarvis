from pathlib import Path

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
              "MORNING_HM", "REPORT_HM", "EVENING_ZONES_HM", "WEEKLY", "MSG_LEVEL"):
        assert f"{k}=" in ex, k


def test_env_example_parses_into_settings(tmp_path):
    from agent.config import Settings
    s = Settings(_env_file=str(ROOT / ".env.example"))
    assert s.model_fast == "claude-haiku-5-5" and s.daily_budget_usd == 1.0 and s.quiet_range()[1].hour == 8
    assert s.owner_id == 0 and s.problems()                                # пустой .env сообщает, что заполнить
