<#
.SYNOPSIS  Starts NEXUS AI. Opens http://127.0.0.1:8000 (backend + built web UI).
.USAGE     powershell -ExecutionPolicy Bypass -File scripts\start.ps1          (normal)
           powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Dev     (also starts the hot-reload UI on http://localhost:5173)
#>
param([switch]$Dev, [switch]$NoBrowser, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "Run scripts\setup.ps1 first." -ForegroundColor Red; exit 1 }
$run = Join-Path $Root "data\run"; $logs = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $run, $logs | Out-Null

# already running?
$pidFile = Join-Path $run "backend.pid"
if (Test-Path $pidFile) {
    $old = Get-Content $pidFile
    if (Get-Process -Id $old -ErrorAction SilentlyContinue) { Write-Host "NEXUS is already running (PID $old). Use scripts\stop.ps1 first." -ForegroundColor Yellow; exit 0 }
}
if ((-not (Test-Path "frontend\dist")) -and (Get-Command npm -ErrorAction SilentlyContinue)) { Push-Location frontend; npm run build; Pop-Location }

$be = Start-Process -FilePath $venvPy -ArgumentList @("-m", "nexus.cli", "serve", "--host", "127.0.0.1", "--port", $Port) -WorkingDirectory (Join-Path $Root "backend") `
      -RedirectStandardOutput (Join-Path $logs "backend.log") -RedirectStandardError (Join-Path $logs "backend.err.log") -WindowStyle Hidden -PassThru
$be.Id | Set-Content $pidFile
$url = "http://127.0.0.1:$Port"
if ($Dev) {
    $fe = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory (Join-Path $Root "frontend") `
          -RedirectStandardOutput (Join-Path $logs "frontend.log") -RedirectStandardError (Join-Path $logs "frontend.err.log") -WindowStyle Hidden -PassThru
    $fe.Id | Set-Content (Join-Path $run "frontend.pid")
    $url = "http://localhost:5173"
}
Write-Host "Starting NEXUS..." -NoNewline
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2; if ($h.ok) { break } } catch { Write-Host "." -NoNewline }
}
Write-Host ""
try { $null = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2; Write-Host "NEXUS is running: $url" -ForegroundColor Green; Write-Host "Logs: data\logs   Stop with: scripts\stop.ps1" }
catch { Write-Host "NEXUS did not start. Look at data\logs\backend.err.log and docs\TROUBLESHOOTING.md" -ForegroundColor Red; exit 1 }
if (-not $NoBrowser) { Start-Process $url }
