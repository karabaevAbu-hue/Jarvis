"""Понятные причины сбоев Telegram для самопроверки и журнала.

Ошибки сопоставляются по ИМЕНИ класса и по тексту, а не по импорту pyrogram: модуль работает без
установленной библиотеки (в CI) и не ломается, если форк переименует базовые классы.
"""
from __future__ import annotations

import asyncio
import sqlite3

SESSION_FILE = "data\\consigliere.session"

# имя класса ошибки -> что делать
_BY_NAME: dict[str, str] = {
    "AuthKeyUnregistered": "сессия больше не действует (Telegram её разлогинил)",
    "AuthKeyInvalid": "файл сессии повреждён",
    "SessionRevoked": "сессию завершили из настроек Telegram (Устройства)",
    "SessionExpired": "сессия устарела",
    "UserDeactivated": "аккаунт ассистента удалён или деактивирован",
    "UserDeactivatedBan": "аккаунт ассистента заблокирован Telegram",
    "PhoneNumberBanned": "номер аккаунта ассистента заблокирован Telegram",
}
_RELOGIN = f"Удали файл {SESSION_FILE} и выполни start_agent.bat --login заново."


def _name(e: BaseException) -> str:
    return type(e).__name__


def explain_tg_error(e: BaseException) -> str:
    """Одна-две фразы по-русски: что случилось и что сделать."""
    n, text = _name(e), str(e)
    low = text.lower()
    if isinstance(e, sqlite3.OperationalError) or "database is locked" in low:
        return ("файл сессии занят: ассистент уже запущен (или завис процесс python). "
                "Останови его (диспетчер задач, процесс python; задание «Consigliere» отключить) и повтори")
    if isinstance(e, sqlite3.DatabaseError):                    # OperationalError «locked» разобран выше
        return f"файл сессии повреждён ({text}). {_RELOGIN}"
    if n == "AuthKeyDuplicated":
        return ("эту же сессию использует другой запущенный ассистент или другой компьютер, Telegram разорвал её. "
                f"Оставь ассистента только в одном месте. {_RELOGIN}")
    if n in ("UserDeactivatedBan", "PhoneNumberBanned"):
        return f"{_BY_NAME[n]}: вход невозможен. Нужен другой аккаунт и другая SIM (раздел 9 ТЗ)"
    if n in _BY_NAME:
        return f"{_BY_NAME[n]}. {_RELOGIN}"
    if n == "ApiIdInvalid" or "api_id/api_hash" in low:
        return "TG_API_ID или TG_API_HASH не подходят: перепиши их с my.telegram.org (API development tools) без пробелов"
    if n == "FloodWait" or "floodwait" in n.lower() or ("flood" in low and "wait" in low):
        sec = getattr(e, "value", None)
        return f"Telegram просит подождать{f' {int(sec)} с' if sec else ''} (FloodWait): слишком много запросов, повтори позже"
    if n in ("PeerIdInvalid", "UserIdInvalid", "UsernameNotOccupied", "UsernameInvalid"):
        return owner_unknown_text()
    if n == "UserIsBlocked":
        return "ты заблокировал аккаунт ассистента: разблокируй его в Telegram (Настройки, Конфиденциальность, Чёрный список)"
    if n in ("UserPrivacyRestricted", "ChatWriteForbidden", "Forbidden"):
        return "Telegram не разрешает ассистенту писать тебе: проверь настройки конфиденциальности и что чат не заблокирован"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return ("Telegram не отвечает: нет интернета или Telegram недоступен с этого сервера. "
                "Проверь, открывается ли интернет на сервере, и повтори")
    if isinstance(e, ConnectionError) or isinstance(e, OSError):
        return f"нет соединения с Telegram: {text or n}. Проверь интернет и брандмауэр сервера"
    return f"{n}: {text}" if text else n


def owner_unknown_text() -> str:
    return ("аккаунт ассистента ещё не знает тебя: открой Telegram со своего аккаунта (Абу), найди аккаунт ассистента "
            "и напиши ему любое сообщение, например «привет». После этого повтори проверку")
