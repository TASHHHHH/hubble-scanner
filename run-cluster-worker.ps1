param(
    [Parameter(Mandatory=$true)][string]$Server,
    [string]$Worker=$env:COMPUTERNAME,
    [string]$WorkDir='work\cluster-worker'
)
$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
if (!$env:HUBBLE_QUEUE_TOKEN) { throw 'Set HUBBLE_QUEUE_TOKEN to the coordinator token first' }
& .\.venv\Scripts\python.exe -u -B cluster_worker.py --server $Server --worker $Worker --work-dir $WorkDir
exit $LASTEXITCODE
