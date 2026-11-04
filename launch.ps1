$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run .\setup.ps1 first.' }
& $python (Join-Path $PSScriptRoot 'app.py')
