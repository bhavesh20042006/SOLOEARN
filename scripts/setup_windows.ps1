$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "[SOLOEARN] Checking Python..." -ForegroundColor Cyan
$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) { py -3.11 --version 2>$null; if ($LASTEXITCODE -ne 0) { py -3 --version } }
else { python --version }

if (-not (Test-Path ".venv")) {
    Write-Host "[SOLOEARN] Creating local virtual environment..." -ForegroundColor Cyan
    if ($python) { py -3 -m venv .venv } else { python -m venv .venv }
}

Write-Host "[SOLOEARN] Installing free Python dependencies..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

Write-Host "" 
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: run .\scripts\run_windows.ps1" -ForegroundColor Green
Write-Host "Ollama is optional for v0.1. See README.md for local model setup." -ForegroundColor Yellow
