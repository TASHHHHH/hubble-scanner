param([switch]$AI)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
py -3.11 -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 with Tcl/Tk first.' }
$req = if ($AI) { 'requirements-ai.txt' } else { 'requirements-core.txt' }
& .\.venv\Scripts\python.exe -m pip install --use-feature=truststore -r $req
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .\.venv\Scripts\python.exe -m unittest discover -v
if ($LASTEXITCODE -ne 0) { throw 'Validation failed.' }
Write-Host 'Ready. Run .\launch.ps1'
