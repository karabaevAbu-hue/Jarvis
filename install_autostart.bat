@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto :no_venv
schtasks /Create /F /TN "Consigliere" /TR "wscript.exe \"%CD%\start_agent_hidden.vbs\"" /SC ONLOGON /RL LIMITED
if errorlevel 1 goto :failed
echo Автозапуск включён: ассистент стартует при входе в Windows.
echo Если он упадёт, перезапуск с паузой 10 с, 30 с, 60 с, потом 5 минут. При ошибке настроек или нет сессии Telegram
echo перезапуски прекращаются: в data\assistant.log причина, на экране окно с сообщением.
echo Остановить:  schtasks /End /TN Consigliere   (процесс python.exe, если остался, закрой в диспетчере задач)
echo Запустить:   schtasks /Run /TN Consigliere
echo Не забудь: автовход в Windows, сон "никогда", ярлык MT5 в shell:startup, Windows Update - активные часы.
echo Подробно: docs\DEPLOY_SERVER.md
pause
exit /b 0

:no_venv
echo [ОШИБКА] Нет окружения .venv. Сначала запусти install.bat.
pause
exit /b 4

:failed
echo [ОШИБКА] Не удалось создать задание автозапуска. Запусти этот файл от имени администратора: правая кнопка, "Запуск от имени администратора".
pause
exit /b 1
