@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
)

set "PORT=5019"
set "MOSS_DATABASE=%cd%\data\app_5019_clean.db"

".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m flask --app app run --host 127.0.0.1 --port 5019
endlocal
