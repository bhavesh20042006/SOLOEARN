@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run SETUP_SOLOEARN.bat first.
  pause
  exit /b 1
)
start "SOLOEARN Discovery Scheduler" /min powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_scheduler_windows.ps1"
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
