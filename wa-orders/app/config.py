"""Настройки из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"


class ConfigError(Exception):
    """Ошибка в .env — понятное сообщение для человека."""


@dataclass(frozen=True)
class Config:
    tg_token: str
    owner_id: int
    operator_ids: frozenset[int]
    green_api_url: str
    green_media_url: str
    green_id_instance: str
    green_api_token: str
    tz: ZoneInfo
    developer_tariff: bool
    db_path: Path

    @property
    def max_groups(self) -> int | None:
        """На бесплатном тарифе Developer GREEN-API работает только с 3 чатами."""
        return 3 if self.developer_tariff else None

    @property
    def admin_ids(self) -> frozenset[int]:
        return self.operator_ids | {self.owner_id}

    def secrets(self) -> list[str]:
        return [s for s in (self.tg_token, self.green_api_token) if s]


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"В .env не заполнено {name}")
    return value


def _int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должен быть числом, сейчас: {raw!r}") from None


def load_config(env_path: Path | None = None) -> Config:
    load_dotenv(env_path or BASE_DIR / ".env", encoding="utf-8-sig", override=False)

    owner_id = _int("OWNER_ID", _required("OWNER_ID"))
    operators_raw = os.getenv("OPERATOR_IDS", "").replace(" ", "")
    operator_ids = frozenset(_int("OPERATOR_IDS", x) for x in operators_raw.split(",") if x)

    tariff = os.getenv("GREEN_TARIFF", "developer").strip().lower()
    if tariff not in ("developer", "business"):
        raise ConfigError("GREEN_TARIFF должен быть developer или business")

    tz_name = os.getenv("APP_TZ", "Asia/Bishkek").strip() or "Asia/Bishkek"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ConfigError(
            f"Неизвестный часовой пояс {tz_name!r}. На Windows нужен пакет tzdata (pip install tzdata)."
        ) from None

    return Config(
        tg_token=_required("TG_TOKEN"),
        owner_id=owner_id,
        operator_ids=operator_ids,
        green_api_url=_required("GREEN_API_URL").rstrip("/"),
        green_media_url=_required("GREEN_MEDIA_URL").rstrip("/"),
        green_id_instance=_required("GREEN_ID_INSTANCE"),
        green_api_token=_required("GREEN_API_TOKEN"),
        tz=tz,
        developer_tariff=tariff == "developer",
        db_path=DATA_DIR / "bot.db",
    )
