@echo off
rem Запуск бота в фоне БЕЗ консольного окна (pythonw) — процесс не привязан к
rem консоли, поэтому не умирает при закрытии терминала (CTRL_CLOSE_EVENT,
rem код выхода 0xC000013A). Используется как задача Планировщика (watchdog):
rem при повторном вызове скрипт ничего не делает, если бот уже работает.
setlocal
cd /d "%~dp0"

rem --- защита от второго экземпляра: один токен = один polling ---
tasklist /fi "imagename eq pythonw.exe" /nh 2>nul | find /i "pythonw.exe" >nul
if not errorlevel 1 (
    echo Bot is already running, nothing to do.
    exit /b 0
)

if not exist "logs" mkdir "logs"
set PYTHONIOENCODING=utf-8

rem pythonw.exe — оконная подсистема: консоль не создаётся, stderr/stdout пишем в файлы
start "tele_eng_bot" /b ".venv\Scripts\pythonw.exe" "main.py" 1>>"logs\console_out.txt" 2>>"logs\console_err.txt"
echo Bot started in background (pythonw, no console window).
endlocal
