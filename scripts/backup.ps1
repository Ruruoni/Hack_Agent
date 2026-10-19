<#  .SYNOPSIS  Writes data\backups\cli-backup.zip (database + settings). Restore with: python -m nexus.cli restore <zip> (stop NEXUS first). #>
$Root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Root "backend")
& (Join-Path $Root ".venv\Scripts\python.exe") -m nexus.cli backup
