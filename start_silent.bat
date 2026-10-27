@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [%date% %time%] ERROR: .venv not found. Create it with py -3.11 -m venv .venv and install requirements.txt.>>"server.log"
  exit /b 1
)
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000 >>"server.log" 2>&1
