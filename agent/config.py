"""Настройки ассистента: .env + проверка (раздел 16 ТЗ)."""
from __future__ import annotations

import os
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


def _default_bridge_root() -> Path:
    """Папка моста по умолчанию: Common\\Files\\MrCasino (на Windows) или ./data/bridge."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files" / "MrCasino"
    return ROOT / "data" / "bridge"


def parse_hm(value: str) -> time | None:
    """'09:30' -> time(9, 30); пусто -> None."""
    value = (value or "").strip()
    if not value:
        return None
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), env_file_encoding="utf-8",
                                      extra="ignore", case_sensitive=False)

    # Telegram
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_phone: str = ""
    owner_id: int = 0
    owner_name: str = "Абу"

    # Claude
    anthropic_api_key: str = ""
    model_fast: str = "claude-haiku-5-5"
    model_smart: str = "claude-sonnet-5-5"
    price_in_fast: float = 0.10
    price_out_fast: float = 0.50
    price_in_smart: float = 2.00
    price_out_smart: float = 10.00
    # множители цены входа для кэша промпта (запись / чтение)
    cache_write_mult: float = 1.25
    cache_read_mult: float = 0.10
    daily_budget_usd: float = 1.00
    # серверный fallback при отказе классификаторов; нужен только для MODEL_SMART на Claude API
    smart_server_fallback: bool = True

    # Мост
    bridge_key: str = ""
    bridge_root: str = ""
    bridge_robot: str = ""       # папка <login>_<magic>; пусто = единственная найденная или свежая по status.json
    mt5_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    mt5_autorestart: bool = True
    symbol: str = ""

    # Время
    tz: str = "Asia/Bishkek"
    quiet_hours: str = "00:00-08:00"
    morning_hm: str = "09:30"
    report_hm: str = "23:05"
    evening_zones_hm: str = "22:30"
    weekly: str = "Sat 10:00"

    # Голос
    voice: str = "ru-RU-DmitryNeural"
    voice_trades: bool = False
    stt_model: str = "small"
    stt_device: str = "auto"
    call_on_critical: bool = True
    call_max_min: int = 15

    msg_level: int = 1

    # Пути данных
    data_dir: str = str(ROOT / "data")

    @field_validator("tg_api_id", "owner_id", mode="before")
    @classmethod
    def _empty_int(cls, v):
        return 0 if v in ("", None) else v

    # ---- удобные свойства ----
    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def bridge_path(self) -> Path:
        return Path(self.bridge_root) if self.bridge_root else _default_bridge_root()

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "assistant.db"

    @property
    def session_path(self) -> Path:
        return Path(self.data_dir) / "consigliere"

    def quiet_range(self) -> tuple[time, time]:
        a, b = self.quiet_hours.split("-")
        return parse_hm(a), parse_hm(b)  # type: ignore[return-value]

    def problems(self) -> list[str]:
        """Что не заполнено. Пустой список = можно запускать."""
        out: list[str] = []
        if not self.tg_api_id or not self.tg_api_hash:
            out.append("TG_API_ID / TG_API_HASH не заполнены (my.telegram.org)")
        if not self.owner_id:
            out.append("OWNER_ID не заполнен (узнать через @userinfobot)")
        if not self.anthropic_api_key:
            out.append("ANTHROPIC_API_KEY не заполнен: ассистент будет отвечать шаблонами без ИИ")
        if not self.bridge_key:
            out.append("BRIDGE_KEY пуст: робот не примет команды")
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
