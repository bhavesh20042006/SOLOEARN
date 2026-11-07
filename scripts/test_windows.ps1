$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run setup_windows.ps1 first." }
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
