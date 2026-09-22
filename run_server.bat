@echo off
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" main.py >> server.log 2>&1
) else (
  python main.py >> server.log 2>&1
)
