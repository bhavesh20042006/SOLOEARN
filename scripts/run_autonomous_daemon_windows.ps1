<#
.SYNOPSIS
    Windows Operational Tooling & Supervisor for SOLOEARN 100% Autonomous Revenue Auto-Pilot Daemon.

.DESCRIPTION
    Provides full lifecycle management for the SOLOEARN 24/7 background revenue daemon:
    - Foreground execution with live interactive banners
    - Detached background execution (headless 24/7 operations)
    - Watchdog auto-restart supervisor against crashes
    - Status telemetry dashboard inspection
    - Automated SQLite database maintenance (WAL checkpoint, backup, vacuum)
    - Live log tailing and graceful shutdown

.PARAMETER Action
    start       : Run the daemon in foreground (default)
    background  : Run daemon detached in background
    stop        : Gracefully stop the running background daemon
    restart     : Stop and restart the daemon
    status      : Display operational health telemetry and process status
    once        : Run a single autonomous cycle and exit
    maintenance : Execute complete database maintenance suite
    checkpoint  : Perform immediate SQLite WAL checkpoint
    backup      : Perform immediate automated database backup
    vacuum      : Perform immediate SQLite database vacuum
    logs        : Display recent daemon execution logs

.PARAMETER Mode
    sandbox     : Safe simulation mode with sandbox leads/UPI webhooks (default)
    live        : Production mode with real outbound outreach and banking

.PARAMETER Interval
    Polling interval in seconds between sweeps (default: 60)

.PARAMETER Watchdog
    Enables automatic supervisor restart loop if daemon process ever crashes

.PARAMETER TailLines
    Number of log lines to show when inspecting logs (default: 40)
#>

[CmdletBinding()]
param(
    [ValidateSet('start', 'background', 'stop', 'restart', 'status', 'once', 'maintenance', 'checkpoint', 'backup', 'vacuum', 'logs')]
    [string]$Action = 'start',

    [ValidateSet('sandbox', 'live')]
    [string]$Mode = 'sandbox',

    [int]$Interval = 60,

    [switch]$Watchdog,

    [int]$TailLines = 40
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host '[ERROR] Virtual environment not found at: ' $Python -ForegroundColor Red
    Write-Host 'Please run SETUP_SOLOEARN.bat or setup_windows.ps1 first.' -ForegroundColor Yellow
    exit 1
}

$DaemonScript = Join-Path $ProjectRoot 'scripts\run_100_percent_autonomous_daemon.py'
$HealthFile   = Join-Path $ProjectRoot 'data\daemon_health.json'
$LogFile      = Join-Path $ProjectRoot 'data\logs\autonomous_daemon.log'

# Environment Setup
$env:SOLOEARN_AUTONOMOUS_OUTREACH = 'true'
$env:PYTHONPATH = $ProjectRoot

$ModeFlag = if ($Mode -eq 'live') { '--live' } else { '--sandbox' }

function Get-RunningDaemonProcess {
    if (Test-Path $HealthFile) {
        try {
            $rawJson = Get-Content $HealthFile -Raw
            $json = ConvertFrom-Json $rawJson
            if ($json.pid -and $json.pid -gt 0) {
                $proc = Get-Process -Id $json.pid -ErrorAction SilentlyContinue
                if ($proc -and ($proc.ProcessName -match 'python')) {
                    return @{ Process = $proc; Telemetry = $json }
                }
            }
        } catch {}
    }
    # Fallback search for python process running daemon script
    $procs = Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%run_100_percent_autonomous_daemon.py%'" -ErrorAction SilentlyContinue
    if ($procs) {
        $p = Get-Process -Id $procs[0].ProcessId -ErrorAction SilentlyContinue
        if ($p) {
            return @{ Process = $p; Telemetry = $null }
        }
    }
    return $null
}

switch ($Action) {
    'status' {
        & $Python $DaemonScript --status
        $running = Get-RunningDaemonProcess
        if ($running) {
            $p = $running.Process
            $memMb = [math]::Round($p.WorkingSet64 / 1MB, 2)
            Write-Host (' [OS Process]         PID ' + $p.Id + ' | Working Set Memory: ' + $memMb + ' MB') -ForegroundColor Cyan
        } else {
            Write-Host ' [OS Process]         No running background daemon process detected.' -ForegroundColor DarkGray
        }
        Write-Host ''
    }

    'once' {
        Write-Host ('[SOLOEARN] Executing single autonomous sweep (' + $Mode + ' mode)...') -ForegroundColor Cyan
        & $Python $DaemonScript $ModeFlag --once
    }

    'checkpoint' {
        Write-Host '[SOLOEARN] Triggering SQLite Write-Ahead-Log (WAL) checkpoint...' -ForegroundColor Cyan
        & $Python $DaemonScript --checkpoint
    }

    'backup' {
        Write-Host '[SOLOEARN] Triggering automated SQLite database backup...' -ForegroundColor Cyan
        & $Python $DaemonScript --backup
    }

    'vacuum' {
        Write-Host '[SOLOEARN] Triggering SQLite database VACUUM optimization...' -ForegroundColor Cyan
        & $Python $DaemonScript --vacuum
    }

    'maintenance' {
        Write-Host '[SOLOEARN] Running complete database maintenance suite...' -ForegroundColor Cyan
        & $Python $DaemonScript --maintenance
    }

    'logs' {
        if (-not (Test-Path $LogFile)) {
            Write-Host ('[NOTICE] Log file not found at: ' + $LogFile) -ForegroundColor Yellow
        } else {
            Write-Host ('=== Last ' + $TailLines + ' Lines of Daemon Log (' + $LogFile + ') ===') -ForegroundColor Cyan
            Get-Content $LogFile -Tail $TailLines
        }
    }

    'stop' {
        Write-Host '[SOLOEARN] Stopping running daemon...' -ForegroundColor Yellow
        $running = Get-RunningDaemonProcess
        if (-not $running) {
            Write-Host '[INFO] No active autonomous daemon process found.' -ForegroundColor DarkGray
        } else {
            $p = $running.Process
            Write-Host ('Terminating daemon process PID ' + $p.Id + '...') -ForegroundColor Yellow
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            Write-Host '[SUCCESS] Autonomous daemon stopped.' -ForegroundColor Green
        }
    }

    'restart' {
        Write-Host '[SOLOEARN] Restarting daemon...' -ForegroundColor Yellow
        $running = Get-RunningDaemonProcess
        if ($running) {
            Stop-Process -Id $running.Process.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
        Write-Host '[SOLOEARN] Launching new daemon process in foreground...' -ForegroundColor Green
        & $Python $DaemonScript $ModeFlag --interval $Interval
    }

    'background' {
        $running = Get-RunningDaemonProcess
        if ($running) {
            Write-Host ('[WARNING] Daemon is already running under PID ' + $running.Process.Id + '!') -ForegroundColor Yellow
            Write-Host 'Use .\scripts\run_autonomous_daemon_windows.ps1 -Action status to check health.'
            exit 0
        }

        Write-Host '==============================================================================' -ForegroundColor Cyan
        Write-Host ' [*] LAUNCHING SOLOEARN 100% AUTONOMOUS DAEMON IN BACKGROUND' -ForegroundColor Green
        Write-Host '==============================================================================' -ForegroundColor Cyan
        Write-Host (' Mode:            ' + $Mode)
        Write-Host (' Sweep Interval:  ' + $Interval + ' seconds')
        Write-Host (' Logs:            ' + $LogFile)
        Write-Host (' Telemetry:       ' + $HealthFile)
        Write-Host '------------------------------------------------------------------------------' -ForegroundColor Gray

        $argList = @($DaemonScript, $ModeFlag, '--interval', $Interval.ToString())
        $proc = Start-Process -FilePath $Python -ArgumentList $argList -WindowStyle Hidden -PassThru

        Start-Sleep -Seconds 3
        if ($proc -and (-not $proc.HasExited)) {
            Write-Host ('[SUCCESS] Autonomous background daemon started under PID ' + $proc.Id + '!') -ForegroundColor Green
            Write-Host 'To monitor:   .\scripts\run_autonomous_daemon_windows.ps1 -Action status' -ForegroundColor Cyan
            Write-Host 'To tail logs: .\scripts\run_autonomous_daemon_windows.ps1 -Action logs' -ForegroundColor Cyan
            Write-Host 'To stop:      .\scripts\run_autonomous_daemon_windows.ps1 -Action stop' -ForegroundColor Yellow
        } else {
            Write-Host ('[ERROR] Daemon failed to start in background. Check ' + $LogFile + ' for details.') -ForegroundColor Red
            exit 1
        }
    }

    'start' {
        $running = Get-RunningDaemonProcess
        if ($running) {
            Write-Host ('[WARNING] A daemon instance is already active under PID ' + $running.Process.Id + '!') -ForegroundColor Yellow
            $choice = Read-Host 'Do you want to stop the existing process and continue? (y/n)'
            if ($choice -eq 'y') {
                Stop-Process -Id $running.Process.Id -Force
                Start-Sleep -Seconds 1
            } else {
                exit 0
            }
        }

        if ($Watchdog) {
            Write-Host '==============================================================================' -ForegroundColor Cyan
            Write-Host ' [*] SOLOEARN AUTONOMOUS DAEMON - WATCHDOG SUPERVISOR ACTIVE' -ForegroundColor Green
            Write-Host '==============================================================================' -ForegroundColor Cyan
            Write-Host ' Auto-restart enabled: Any abnormal termination will be recovered.' -ForegroundColor White
            Write-Host ' Press Ctrl+C at any time to halt the supervisor.' -ForegroundColor Yellow
            Write-Host '------------------------------------------------------------------------------' -ForegroundColor Gray

            $restarts = 0
            while ($true) {
                try {
                    & $Python $DaemonScript $ModeFlag --interval $Interval
                    $code = $LASTEXITCODE
                    if ($code -eq 0) {
                        Write-Host '[WATCHDOG] Daemon shut down cleanly (exit code 0).' -ForegroundColor Green
                        break
                    } else {
                        $restarts++
                        Write-Host ('[WATCHDOG WARNING] Daemon terminated with error code ' + $code + '. Restart count: ' + $restarts + ' in 5s...') -ForegroundColor Red
                        Start-Sleep -Seconds 5
                    }
                } catch {
                    $restarts++
                    $errText = $_.ToString()
                    Write-Host ('[WATCHDOG EXCEPTION] ' + $errText + '. Auto-restarting daemon in 5s...') -ForegroundColor Red
                    Start-Sleep -Seconds 5
                }
            }
        } else {
            & $Python $DaemonScript $ModeFlag --interval $Interval
        }
    }
}
