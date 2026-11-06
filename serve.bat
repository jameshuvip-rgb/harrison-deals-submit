@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv 2>nul
  if errorlevel 1 python -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
if not defined HD_SUBMIT_HOST set HD_SUBMIT_HOST=0.0.0.0
if not defined HD_SUBMIT_PORT set HD_SUBMIT_PORT=8788
python app.py
