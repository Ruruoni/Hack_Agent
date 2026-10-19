<#  .SYNOPSIS  Stops NEXUS AI (backend and, if started, the dev UI). Safe to run when nothing is running. #>
$Root = Split-Path -Parent $PSScriptRoot
foreach ($n in @("backend", "frontend")) {
    $f = Join-Path $Root "data\run\$n.pid"
    if (Test-Path $f) {
        $p = [int](Get-Content $f)
        if (Get-Process -Id $p -ErrorAction SilentlyContinue) { & taskkill /PID $p /T /F | Out-Null; Write-Host "Stopped $n (PID $p)" } else { Write-Host "$n was not running" }
        Remove-Item $f -Force
    }
}
