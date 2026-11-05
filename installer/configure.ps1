param([Parameter(Mandatory=$true)][string]$Runtime, [switch]$AI)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA 'HubbleScanner'
New-Item -ItemType Directory -Force $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir 'setup.log') -Force
try {
    # Prefer a working 64-bit 3.11 with Tcl/Tk, including python.org installations without py.exe.
    function Find-Python {
        $paths = @()
        if (Get-Command py.exe -ErrorAction SilentlyContinue) {
            try { $paths += & py.exe -3.11 -c 'import sys; print(sys.executable)' 2>$null } catch { }
        }
        $paths += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
        foreach ($path in $paths) {
            if ($path -and (Test-Path -LiteralPath $path)) {
                & $path -c 'import sys,tkinter,venv; assert sys.version_info[:2]==(3,11) and sys.maxsize>2**32'
                if ($LASTEXITCODE -eq 0) { return $path }
            }
        }
    }
    $python = Find-Python
    if (!$python) {
        if ((Get-FileHash -LiteralPath $Runtime).Hash -ne '5EE42C4EEE1E6B4464BB23722F90B45303F79442DF63083F05322F1785F5FDDE') {
            throw 'The bundled Python installer failed its integrity check.'
        }
        $result = Start-Process -FilePath $Runtime -ArgumentList '/quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 Include_tcltk=1 PrependPath=0' -WindowStyle Hidden -PassThru -Wait
        if ($result.ExitCode -notin 0,3010) { throw "Python installation failed ($($result.ExitCode))." }
        $python = Find-Python
        if (!$python) { throw 'Python 3.11 with Tcl/Tk could not be located.' }
    }
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment setup failed.' }
    $requirements = if ($AI) { 'requirements-ai.txt' } else { 'requirements-core.txt' }
    & .\.venv\Scripts\python.exe -m pip install --use-feature=truststore -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Package download or installation failed. Check your internet connection.' }
    & .\.venv\Scripts\python.exe -c 'import app, field_search, cluster_worker'
    if ($LASTEXITCODE -ne 0) { throw 'Application startup check failed.' }
} catch {
    Write-Host $_
    exit 1
} finally {
    Stop-Transcript
}
