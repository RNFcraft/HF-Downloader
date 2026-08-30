param([switch]$CheckOnly)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$pythonwExe = Join-Path $venvDir "Scripts\pythonw.exe"

Set-Location -LiteralPath $projectDir
Write-Host "HF Downloader - checking environment" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $systemPython) { $systemPython = Get-Command py.exe -ErrorAction SilentlyContinue }
    if (-not $systemPython) {
        throw "Python 3.10+ was not found. Install Python and enable Add Python to PATH."
    }
    Write-Host "Creating an isolated Python environment..." -ForegroundColor Yellow
    if ($systemPython.Name -eq "py.exe") {
        & $systemPython.Source -3 -m venv $venvDir
    } else {
        & $systemPython.Source -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}

$versionText = & $pythonExe -c "import sys; print(str(sys.version_info.major) + '.' + str(sys.version_info.minor))"
$versionParts = $versionText.Split(".")
if ([int]$versionParts[0] -lt 3 -or ([int]$versionParts[0] -eq 3 -and [int]$versionParts[1] -lt 10)) {
    throw "Python 3.10 or newer is required; found $versionText."
}

Write-Host "Checking and updating dependencies..." -ForegroundColor Yellow
& $pythonExe -m pip install --disable-pip-version-check --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Warning "pip update was skipped because the package server is unavailable."
}
& $pythonExe -m pip install --disable-pip-version-check --quiet --upgrade --upgrade-strategy only-if-needed -r (Join-Path $projectDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Dependency update failed. Checking whether the installed versions are usable..."
    & $pythonExe -c "import huggingface_hub, hf_xet"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependencies are missing. Check internet, proxy, or antivirus settings and retry."
    }
    Write-Warning "Using installed dependencies; update will be retried on the next launch."
}

$defaultDestination = "D:\projects\aiagent\hf_models"
if (-not (Test-Path -LiteralPath $defaultDestination)) {
    New-Item -ItemType Directory -Path $defaultDestination -Force | Out-Null
}

if ($CheckOnly) {
    Write-Host "Environment check passed. The application is ready." -ForegroundColor Green
    exit 0
}
Write-Host "Starting the application..." -ForegroundColor Green
Start-Process -FilePath $pythonwExe -ArgumentList (Join-Path $projectDir "main.py") -WorkingDirectory $projectDir
