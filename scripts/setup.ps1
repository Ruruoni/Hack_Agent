<#
.SYNOPSIS  One-time setup for NEXUS AI on Windows: Python environment, packages, web UI build, database.
.USAGE     From the project folder:   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
           Add -WithTraining to also install the OPTIONAL fine-tuning libraries (several GB).
           Safe to re-run: existing .venv, .env and data\ are kept.
#>
param([switch]$WithTraining)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path "backend\requirements.txt")) { Fail "Run this script from a complete checkout (backend\requirements.txt not found under $Root)." }

# Returns @{ Exe = "py"; Args = @("-3.12") } (or Exe = "python"), or $null. Plain objects only - no nested arrays.
function Find-Python {
    $candidates = @(
        @{ Exe = "py";     Args = @("-3.12") }, @{ Exe = "py"; Args = @("-3.11") }, @{ Exe = "py"; Args = @("-3.10") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $c.Exe @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -and ([version]$v -ge [version]"3.10")) { return $c }
        } catch { }
    }
    return $null
}

Write-Host "== 1/6 Checking Python" -ForegroundColor Cyan
$py = Find-Python
if (-not $py) {
    Write-Host "Python 3.10+ was not found." -ForegroundColor Red
    Write-Host "Install Python 3.12 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then close and reopen PowerShell."
    Write-Host "If 'python' opens the Microsoft Store, turn off the Python entries under Settings > Apps > Advanced app settings > App execution aliases."
    exit 1
}
Write-Host ("Using: {0} {1}  ->  {2}" -f $py.Exe, ($py.Args -join " "), (& $py.Exe @($py.Args) --version))

Write-Host "== 2/6 Creating the virtual environment (.venv)" -ForegroundColor Cyan
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $py.Exe @($py.Args) -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Could not create .venv. See docs\TROUBLESHOOTING.md" }
} else { Write-Host ".venv already exists - reusing it." }
& $venvPy -m pip install --upgrade pip --disable-pip-version-check | Out-Null

Write-Host "== 3/6 Installing Python packages (a few minutes)" -ForegroundColor Cyan
& $venvPy -m pip install --disable-pip-version-check -r backend\requirements-dev.txt
if ($LASTEXITCODE -ne 0) { Fail "Package installation failed (scroll up for the first red error). See docs\TROUBLESHOOTING.md > Installation." }
& $venvPy -m pip install --disable-pip-version-check -r backend\requirements-optional.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Optional packages (ChromaDB) could not be installed - NEXUS will use its built-in SQLite vector store. That is fine." -ForegroundColor Yellow }
if ($WithTraining) {
    Write-Host "Installing optional fine-tuning libraries..." -ForegroundColor Cyan
    & $venvPy -m pip install --disable-pip-version-check -r backend\requirements-training.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "Training libraries failed to install (see docs\TROUBLESHOOTING.md). The rest of NEXUS is unaffected." -ForegroundColor Yellow }
}

Write-Host "== 4/6 Creating .env" -ForegroundColor Cyan
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Created .env (edit it, or use the Settings page)." } else { Write-Host ".env already exists - kept." }

Write-Host "== 5/6 Web interface (Node.js)" -ForegroundColor Cyan
$webOk = $false
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Push-Location frontend
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -eq 0) { npm run build; if ($LASTEXITCODE -eq 0) { $webOk = $true } }
    Pop-Location
    if (-not $webOk) { Write-Host "The web UI could not be built (see messages above). The API still works; fix Node/npm and re-run." -ForegroundColor Yellow }
} else {
    Write-Host "Node.js (npm) was not found, so the web UI was not built. Install the LTS version from https://nodejs.org/, reopen PowerShell, and re-run this script." -ForegroundColor Yellow
}

Write-Host "== 6/6 Creating the database" -ForegroundColor Cyan
Push-Location backend
& $venvPy -m nexus.cli init-db
$dbExit = $LASTEXITCODE
Pop-Location
if ($dbExit -ne 0) { Fail "Database initialisation failed. See docs\TROUBLESHOOTING.md" }

Write-Host ""
if ($webOk) { Write-Host "Setup finished." -ForegroundColor Green } else { Write-Host "Setup finished WITHOUT the web UI (see the yellow messages)." -ForegroundColor Yellow }
Write-Host "Next: start LM Studio's server (LM_STUDIO_GUIDE.md), then run  powershell -ExecutionPolicy Bypass -File scripts\start.ps1"
