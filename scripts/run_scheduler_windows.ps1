$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Environment not found. Run .\scripts\setup_windows.ps1 first."
}
& .\.venv\Scripts\python.exe -m core.scheduler_runner
