<#  .SYNOPSIS  Creates (or upgrades) the SQLite database. Safe to run repeatedly - existing data is kept.  #>
$Root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Root "backend")
& (Join-Path $Root ".venv\Scripts\python.exe") -m nexus.cli init-db
