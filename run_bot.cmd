@echo off
rem Start the bot with console logs duplicated into logs\ (Task Scheduler / manual run).
setlocal
cd /d "%~dp0"
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" "main.py" 1>>"logs\console_out.txt" 2>>"logs\console_err.txt"
endlocal
