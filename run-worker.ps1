param(
    [ValidateSet('wiki','mast')][string]$Stage='mast',
    [ValidateRange(0,9)][int]$Worker=0,
    [ValidateRange(1,10)][int]$Workers=8,
    [string]$Catalog='out/catalog.jsonl',
    [string]$WikiGlob='out/wiki_*.jsonl',
    [int]$Limit=0
)
$ErrorActionPreference='Stop'
Set-Location $PSScriptRoot
if ($Worker -ge $Workers) { throw 'Worker must be less than Workers' }
$pythonPath=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install requirements-core.txt first' }
$workerArgs=@('scanner.py',$Stage,'--catalog',$Catalog,'--worker',"$Worker",'--workers',"$Workers",'--limit',"$Limit",'--out',"out/${Stage}_${Worker}.jsonl")
if ($Stage -eq 'mast') { $workerArgs+=@('--wiki-glob',$WikiGlob) }
& $pythonPath @workerArgs
exit $LASTEXITCODE
