<#  .SYNOPSIS  Runs the automated tests (backend: pytest, frontend: vitest).  #>
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
Push-Location backend; & $venvPy -m pytest tests -v; $be = $LASTEXITCODE; Pop-Location
if (Get-Command npm -ErrorAction SilentlyContinue) { Push-Location frontend; npm test; $fe = $LASTEXITCODE; Pop-Location } else { $fe = 0; Write-Host "npm not found - frontend tests skipped" }
exit ([int]($be -ne 0 -or $fe -ne 0))
