@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Please run SETUP_SOLOEARN.bat first.
  pause
  exit /b 1
)
title SOLOEARN 100% Autonomous Revenue Auto-Pilot
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_autonomous_daemon_windows.ps1"
pause
