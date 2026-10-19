<#
.SYNOPSIS  One-time setup for NEXUS AI on Windows (creates the Python environment, installs packages, builds the web UI, creates the database).
.USAGE     Open PowerShell in the project folder and run:   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
           Add -WithTraining to also install the optional fine-tuning libraries (large download).
#>
param([switch]$WithTraining)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    foreach ($cand in @(@("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("python"))) {
        try {
            $exe = $cand[0]; $args2 = @(); if ($cand.Count -gt 1) { $args2 = $cand[1..($cand.Count - 1)] }
            $v = & $exe @args2 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.10") { return ,@($exe) + $args2 }
        } catch { }
    }
    return $null
}

Write-Host "== 1/6 Checking Python" -ForegroundColor Cyan
$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.10+ was not found." -ForegroundColor Red
    Write-Host "Install Python 3.12 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then reopen PowerShell and re-run this script."
    exit 1
}
Write-Host ("Using: " + ($py -join " ") + "  ->  " + (& $py[0] @($py[1..($py.Count)]) --version))

Write-Host "== 2/6 Creating the virtual environment (.venv)" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { & $py[0] @($py[1..($py.Count)]) -m venv .venv }
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null

Write-Host "== 3/6 Installing Python packages (a few minutes)" -ForegroundColor Cyan
& $venvPy -m pip install -r backend\requirements-dev.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Package installation failed. See docs\TROUBLESHOOTING.md" -ForegroundColor Red; exit 1 }
try {
    & $venvPy -m pip install -r backend\requirements-optional.txt
    if ($LASTEXITCODE -ne 0) { throw "optional failed" }
} catch { Write-Host "Optional packages (ChromaDB) could not be installed - NEXUS will use its built-in SQLite vector store. That is fine." -ForegroundColor Yellow }
if ($WithTraining) {
    Write-Host "Installing optional fine-tuning libraries..." -ForegroundColor Cyan
    & $venvPy -m pip install -r backend\requirements-training.txt
}

Write-Host "== 4/6 Creating .env" -ForegroundColor Cyan
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env (edit it or use the Settings page)." }

Write-Host "== 5/6 Web interface (Node.js)" -ForegroundColor Cyan
$npm = Get-Command npm -ErrorAction SilentlyContinue
if ($npm) {
    Push-Location frontend
    npm install --no-audit --no-fund
    npm run build
    Pop-Location
} else {
    Write-Host "Node.js was not found, so the web UI was not built. Install the LTS version from https://nodejs.org/ and run this script again." -ForegroundColor Yellow
}

Write-Host "== 6/6 Creating the database" -ForegroundColor Cyan
Push-Location backend
& $venvPy -m nexus.cli init-db
Pop-Location
Write-Host "`nSetup finished. Next: start LM Studio's server (see LM_STUDIO_GUIDE.md), then run  scripts\start.ps1" -ForegroundColor Green
