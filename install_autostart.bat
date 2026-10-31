@echo off
chcp 65001 >nul
cd /d "%~dp0"
schtasks /Create /F /TN "Consigliere" /TR "wscript.exe \"%CD%\start_agent_hidden.vbs\"" /SC ONLOGON /RL LIMITED
if errorlevel 1 (
  echo Не удалось создать задание. Запусти этот файл от имени администратора.
  exit /b 1
)
echo Автозапуск включён: ассистент стартует при входе в Windows.
echo Не забудь: ярлык MT5 в shell:startup, сон ПК "никогда", Windows Update - активные часы 08:00-02:00.
