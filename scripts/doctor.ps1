<#  .SYNOPSIS  Checks Python, Node, LM Studio, the database and model responses from the command line.  #>
$Root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Root "backend")
& (Join-Path $Root ".venv\Scripts\python.exe") -m nexus.cli doctor
