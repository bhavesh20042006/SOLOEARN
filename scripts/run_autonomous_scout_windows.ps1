# scripts/run_autonomous_scout_windows.ps1
# Launches the SOLOEARN Autonomous Scout & Outreach Daemon in a persistent background console

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Virtual environment not found at $VenvPython. Run SETUP_SOLOEARN.bat first." -ForegroundColor Red
    exit 1
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " Starting SOLOEARN Autonomous Scout & Outreach Daemon" -ForegroundColor Cyan
Write-Host " Mode: Background Daemon (Polling every 60 minutes)" -ForegroundColor Cyan
Write-Host " Press Ctrl+C in this console to stop at any time." -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

& $VenvPython "scripts\run_autonomous_scout_and_pitch.py" --loop --interval 60 --max-pitches 5
