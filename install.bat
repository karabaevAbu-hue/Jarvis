@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "NOPAUSE="
if /i "%~1"=="/nopause" set "NOPAUSE=1"

echo === Установка Консильери ===
echo.

rem --- 1. Python: есть ли он и запускается ли (заглушка Microsoft Store тоже находится через where)
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys" >nul 2>nul
if errorlevel 1 goto :broken_python

rem --- 2. версия Python, 64 бита, ffmpeg: подробности печатает tools\install_helper.py
echo Проверяю Python и ffmpeg...
python -m tools.install_helper --check
if errorlevel 1 goto :fail

rem --- 3. файл .env и ключ моста
python -m tools.install_helper
if errorlevel 1 goto :no_env

rem --- 4. виртуальное окружение
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение .venv ...
  python -m venv .venv
  if errorlevel 1 goto :no_venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto :pip_failed

rem --- 5. основные зависимости: без torch и whisper
echo Ставлю основные зависимости...
pip install -r requirements-windows.txt
if errorlevel 1 goto :pip_failed

rem --- 6. голос: только если в .env VOICE_ENABLED=true
python -m tools.install_helper --voice-enabled
if errorlevel 1 goto :voice_off
echo Голос включён. Ставлю requirements-voice.txt: он тяжёлый, нужно несколько ГБ места и время.
pip install -r requirements-voice.txt
if errorlevel 1 goto :pip_failed
python -m tools.install_helper --models
if errorlevel 1 goto :pip_failed
goto :done

:voice_off
echo Голос выключен, VOICE_ENABLED=false в .env. Torch и whisper не ставлю. Включить позже: см. docs\DEPLOY_SERVER.md.

:done
echo.
echo Готово. Дальше:
echo   1. Открой .env и заполни TG_API_ID, TG_API_HASH, TG_PHONE, OWNER_ID, ANTHROPIC_API_KEY, MT5_PATH.
echo   2. start_agent.bat --login      - код из Telegram и пароль 2FA
echo   3. start_agent.bat --selftest   - проверка Telegram, моста, MT5 и Claude
echo   4. start_agent.bat              - работа, затем install_autostart.bat
call :hold
exit /b 0

:no_python
echo.
echo [ОШИБКА] Python не найден.
echo   Скачай Python 3.12 64-bit с python.org/downloads. При установке отметь "Add python.exe to PATH".
echo   Потом закрой это окно, открой новое и запусти install.bat снова.
goto :fail

:broken_python
echo.
echo [ОШИБКА] Команда python есть, но не запускается. Часто это заглушка Microsoft Store.
echo   Поставь Python 3.12 с python.org/downloads с галочкой "Add python.exe to PATH".
echo   Если заглушка мешает: Параметры, Приложения, Псевдонимы выполнения приложений, выключи python.exe.
goto :fail

:no_env
echo.
echo [ОШИБКА] Не удалось создать файл .env из .env.example. Проверь, что файл .env.example лежит рядом с install.bat.
goto :fail

:no_venv
echo.
echo [ОШИБКА] Не удалось создать виртуальное окружение .venv. Прочитай ошибку выше. Если папка .venv осталась, удали её и повтори.
goto :fail

:pip_failed
echo.
echo [ОШИБКА] Зависимости не установились, причина в тексте выше.
echo   Частые причины: нет интернета, нет места на диске, 32-битный Python.
echo   Исправь и запусти install.bat снова: он продолжит с того же места.
goto :fail

:fail
echo.
echo Установка остановлена.
call :hold
exit /b 1

:hold
if not defined NOPAUSE pause
exit /b 0
