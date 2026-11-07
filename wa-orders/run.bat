@echo off
rem Ручной запуск бота. Остановить: Ctrl+C.
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m app
pause
