# Pregame viewer (Windows): creates .venv in this folder if missing, installs requirements.txt, starts the
# Read-only server and opens the browser. Arguments pass through to server.py:
#   .\start.ps1                 online (MongoDB Atlas, config in viewer.env)
#   .\start.ps1 --offline       serve the snapshot in fixtures\
#   .\start.ps1 --port 8900 --no-browser
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot

$venvDir = Join-Path $PSScriptRoot '.venv'
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
$versionCheck = 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'

function Find-Python {
    foreach ($candidate in @('py -3', 'python', 'python3')) {
        $parts = $candidate -split ' '
        if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
        $pre = @($parts | Select-Object -Skip 1)
        & $parts[0] @pre -c $versionCheck 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

# A .venv copied from another laptop points at that laptop's Python: rebuild it.
if (Test-Path $venvPy) {
    & $venvPy -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[start] .venv does not run on this laptop (copied from another machine?); rebuilding it."
        Remove-Item -Recurse -Force $venvDir
    }
}

$created = $false
if (-not (Test-Path $venvPy)) {
    $python = Find-Python
    if (-not $python) {
        Write-Host "[start] Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/"
        Write-Host "        (tick 'Add python.exe to PATH'), then run this script again."
        exit 1
    }
    Write-Host "[start] creating .venv with '$python' ..."
    $parts = $python -split ' '
    $pre = @($parts | Select-Object -Skip 1)
    & $parts[0] @pre -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Write-Host "[start] could not create .venv"
        exit 1
    }
    $created = $true
}

# Install requirements when the venv is new or requirements.txt changed since the last successful install.
$stamp = Join-Path $venvDir 'requirements.stamp'
$wanted = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'requirements.txt')
$installed = if (Test-Path $stamp) { Get-Content -Raw -LiteralPath $stamp } else { '' }
if ($created -or $wanted -ne $installed) {
    Write-Host "[start] installing requirements.txt into .venv ..."
    & $venvPy -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -eq 0) {
        Set-Content -LiteralPath $stamp -Value $wanted -NoNewline -Encoding utf8
    } else {
        Write-Host "[start] warning: pip install failed (no internet?). --offline still works; online mode needs pymongo."
    }
}

& $venvPy (Join-Path $PSScriptRoot 'server.py') @args
exit $LASTEXITCODE
