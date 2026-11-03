"""Коды выхода ассистента. Их читает присмотрщик автозапуска (agent/supervisor.py)."""
from __future__ import annotations

OK = 0
NO_SETTINGS = 2       # в .env не заполнены TG_API_ID, TG_API_HASH, OWNER_ID: перезапуск не поможет
NO_SESSION = 3        # нет сессии Telegram или вход не удался: нужен start_agent.bat --login
NOT_INSTALLED = 4     # нет окружения .venv: нужен install.bat (возвращает start_agent.bat)

# коды, при которых автоперезапуск бессмыслен: исправить может только человек
FATAL = {
    NO_SETTINGS: "в .env не заполнены настройки (TG_API_ID, TG_API_HASH, OWNER_ID). "
                 "Заполни .env и запусти ассистента снова.",
    NO_SESSION: "нет сессии Telegram или вход не удался (сессию отозвали, файл повреждён или его не было). "
                "Выполни start_agent.bat --login и запусти ассистента снова.",
    NOT_INSTALLED: "не найдено окружение .venv. Запусти install.bat и потом ассистента.",
}
