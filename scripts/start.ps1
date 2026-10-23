<#
.SYNOPSIS  Starts NEXUS AI (backend + built web UI) and opens it in your browser.
.USAGE     powershell -ExecutionPolicy Bypass -File scripts\start.ps1                 (normal: http://127.0.0.1:8000)
           powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8010      (different port)
           powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Dev            (also the hot-reload UI on http://localhost:5173; uses port 8000 for the API)
           -NoBrowser  do not open the browser.
.NOTES     Binds to 127.0.0.1 only. Logs: data\logs\backend.log and backend.err.log. Stop with scripts\stop.ps1.
#>
param([switch]$Dev, [switch]$NoBrowser, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "The Python environment is missing. Run scripts\setup.ps1 first." -ForegroundColor Red; exit 1 }
$run  = Join-Path $Root "data\run"
$logs = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $run, $logs | Out-Null
$pidFile = Join-Path $run "backend.pid"

function Test-PortBusy([int]$p) {
    try { return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop) }
    catch {
        # Get-NetTCPConnection unavailable/failed: fall back to a connect probe
        try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $p); $c.Close(); return $true } catch { return $false }
    }
}

# Already running? (verify the PID really is a Python process, PIDs get reused by Windows)
if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
    $proc = if ($old) { Get-Process -Id $old -ErrorAction SilentlyContinue } else { $null }
    if ($proc -and $proc.ProcessName -like "python*") {
        Write-Host "NEXUS is already running (PID $old). Open http://127.0.0.1:$Port or run scripts\stop.ps1 first." -ForegroundColor Yellow
        exit 0
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue   # stale file
}

if (Test-PortBusy $Port) {
    Write-Host "Port $Port is already in use by another program." -ForegroundColor Red
    Write-Host "See what is using it:   netstat -ano | findstr :$Port     then   tasklist /FI ""PID eq <pid>"""
    Write-Host "Or start NEXUS on another port:   powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Port 8010"
    exit 1
}

if (-not (Test-Path "frontend\dist\index.html") -and -not (Test-Path "frontend\prebuilt\index.html")) {
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "Building the web UI (first start)..." -ForegroundColor Cyan
        Push-Location frontend; npm run build; $b = $LASTEXITCODE; Pop-Location
        if ($b -ne 0) { Write-Host "UI build failed - starting API only." -ForegroundColor Yellow }
    } else { Write-Host "Web UI is not built and Node.js is missing - starting API only (see INSTALL_WINDOWS.md step 2)." -ForegroundColor Yellow }
}

$be = Start-Process -FilePath $venvPy -ArgumentList @("-m", "nexus.cli", "serve", "--host", "127.0.0.1", "--port", "$Port") `
      -WorkingDirectory (Join-Path $Root "backend") -RedirectStandardOutput (Join-Path $logs "backend.log") `
      -RedirectStandardError (Join-Path $logs "backend.err.log") -WindowStyle Hidden -PassThru
$be.Id | Set-Content $pidFile

$url = "http://127.0.0.1:$Port"
if ($Dev) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Write-Host "-Dev needs Node.js/npm." -ForegroundColor Red; Stop-Process -Id $be.Id -Force; Remove-Item $pidFile -Force; exit 1 }
    $env:NEXUS_API = "http://127.0.0.1:$Port"
    $fe = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory (Join-Path $Root "frontend") `
          -RedirectStandardOutput (Join-Path $logs "frontend.log") -RedirectStandardError (Join-Path $logs "frontend.err.log") -WindowStyle Hidden -PassThru
    $fe.Id | Set-Content (Join-Path $run "frontend.pid")
    $url = "http://localhost:5173"
}

Write-Host "Starting NEXUS" -NoNewline
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if ($be.HasExited) { break }
    try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2; if ($h.ok) { $healthy = $true; break } } catch { Write-Host "." -NoNewline }
}
Write-Host ""
if (-not $healthy) {
    Write-Host "NEXUS did not start." -ForegroundColor Red
    $err = Join-Path $logs "backend.err.log"
    if (Test-Path $err) { Write-Host "--- last lines of data\logs\backend.err.log ---"; Get-Content $err -Tail 15 }
    Write-Host "See docs\TROUBLESHOOTING.md"
    if (-not $be.HasExited) { Stop-Process -Id $be.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "NEXUS is running: $url" -ForegroundColor Green
Write-Host "Logs: data\logs      Stop with: powershell -ExecutionPolicy Bypass -File scripts\stop.ps1"
if (-not $NoBrowser) { Start-Process $url }
