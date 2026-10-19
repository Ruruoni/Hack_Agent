<#
.SYNOPSIS  Stops NEXUS AI (backend and, if started with -Dev, the dev UI). Safe to run when nothing is running.
.NOTES     Only processes started by scripts\start.ps1 are stopped (tracked in data\run\*.pid). If you started the backend by hand,
           close its window or press Ctrl+C there.
#>
param([int]$Port = 8000)
$Root = Split-Path -Parent $PSScriptRoot
$any = $false
foreach ($n in @("backend", "frontend")) {
    $f = Join-Path $Root "data\run\$n.pid"
    if (-not (Test-Path $f)) { continue }
    $any = $true
    $p = [int](Get-Content $f -ErrorAction SilentlyContinue)
    if ($p -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) {
        & taskkill /PID $p /T /F | Out-Null       # /T also ends child processes (npm -> node)
        Write-Host "Stopped $n (PID $p)"
    } else { Write-Host "$n was not running (stale PID file removed)" }
    Remove-Item $f -Force -ErrorAction SilentlyContinue
}
if (-not $any) {
    Write-Host "No NEXUS processes were started by scripts\start.ps1."
    try {
        $l = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($l) { Write-Host "Something is still listening on port $Port (PID $($l.OwningProcess)). If that is a NEXUS you started manually, stop it in its own window." -ForegroundColor Yellow }
    } catch { }
}
