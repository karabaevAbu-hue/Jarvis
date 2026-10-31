"""Мелкие общие помощники: время, числа, атомарная запись."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    """2026-11-02T08:15:10Z"""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def local_str(dt: datetime, tz: ZoneInfo | str = "Asia/Bishkek") -> str:
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def local_day(dt: datetime, tz: ZoneInfo | str = "Asia/Bishkek") -> str:
    tz = ZoneInfo(tz) if isinstance(tz, str) else tz
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def write_atomic(path: Path, text: str) -> None:
    """Пишет во временный файл рядом и переименовывает: читатель не увидит половину файла."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fnum(x: float, digits: int = 2) -> str:
    """Число с точкой, без лишнего."""
    return f"{x:.{digits}f}"


def signed(x: float, digits: int = 2) -> str:
    return f"{x:+.{digits}f}".replace("-", "−")
