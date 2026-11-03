@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Без pause: этот файл запускает и скрытый автозапуск (start_agent_hidden.vbs), пауза его зависила бы.
rem Коды выхода: 2 нет настроек .env, 3 нет сессии Telegram, 4 нет окружения .venv (agent\exitcodes.py).
if not exist ".venv\Scripts\activate.bat" (
  echo [ОШИБКА] Не найдено окружение .venv. Сначала запусти install.bat.
  exit /b 4
)
call .venv\Scripts\activate.bat
if /i "%~1"=="--supervised" goto :supervised
python -m agent.main %*
exit /b %errorlevel%

:supervised
rem Автозапуск: перезапуск с нарастающей паузой, остановка при ошибках настройки (agent\supervisor.py)
python -m agent.supervisor
exit /b %errorlevel%
