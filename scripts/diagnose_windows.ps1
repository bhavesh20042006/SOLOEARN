Write-Host "=== SOLOEARN hardware / runtime diagnostics ===" -ForegroundColor Cyan
Write-Host "OS:"
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, OSArchitecture
Write-Host "CPU:"
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
Write-Host "Memory (GB):"
$os = Get-CimInstance Win32_OperatingSystem
[math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
Write-Host "GPU(s):"
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterRAM
Write-Host "Ollama:"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    ollama --version
    ollama list
} else {
    Write-Host "Ollama not installed."
}
Write-Host ""
Write-Host "Paste the CPU and GPU names from this output if we need to tune GPU acceleration." -ForegroundColor Yellow
