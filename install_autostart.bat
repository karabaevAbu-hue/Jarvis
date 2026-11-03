@echo off
chcp 65001 >nul
cd /d "%~dp0"
schtasks /Create /F /TN "Consigliere" /TR "wscript.exe \"%CD%\start_agent_hidden.vbs\"" /SC ONLOGON /RL LIMITED
if errorlevel 1 goto :failed
echo Автозапуск включён: ассистент стартует при входе в Windows.
echo Не забудь: автовход в Windows, сон "никогда", ярлык MT5 в shell:startup, Windows Update - активные часы.
echo Подробно: docs\DEPLOY_SERVER.md
pause
exit /b 0

:failed
echo [ОШИБКА] Не удалось создать задание автозапуска. Запусти этот файл от имени администратора: правая кнопка, "Запуск от имени администратора".
pause
exit /b 1
