@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Без pause: этот файл запускает и скрытый цикл автозапуска (start_agent_hidden.vbs), пауза его зависила бы.
if not exist ".venv\Scripts\activate.bat" (
  echo [ОШИБКА] Не найдено окружение .venv. Сначала запусти install.bat.
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m agent.main %*
