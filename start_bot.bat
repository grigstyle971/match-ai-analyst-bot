@echo off
rem Запуск бота двойным щелчком. Окно должно оставаться открытым, пока бот нужен.
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe bot.py
pause
