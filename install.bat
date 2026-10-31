@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка Консильери ===
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 не найден. Поставь его с python.org и отметь галочку "Add to PATH".
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg не найден. Выполни: winget install Gyan.FFmpeg  и запусти install.bat снова.
  exit /b 1
)
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements-windows.txt
if errorlevel 1 (
  echo Зависимости не установились, см. ошибку выше.
  exit /b 1
)
python -m tools.install_helper
python -m tools.install_helper --models
echo.
echo Готово. Дальше:
echo   1. Открой .env и заполни TG_API_ID, TG_API_HASH, TG_PHONE, OWNER_ID, ANTHROPIC_API_KEY, MT5_PATH.
echo   2. start_agent.bat --login      (код из Telegram и пароль 2FA)
echo   3. start_agent.bat --selftest   (проверка всего)
echo   4. start_agent.bat              (работа), затем install_autostart.bat
