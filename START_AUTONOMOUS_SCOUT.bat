@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Please run SETUP_SOLOEARN.bat first.
  pause
  exit /b 1
)
title SOLOEARN Autonomous Scout & Outreach Daemon
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_autonomous_scout_windows.ps1"
pause
