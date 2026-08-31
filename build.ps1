param([switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
Set-Location -LiteralPath $projectDir

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Run Install-and-Run.bat first to create .venv."
}

& $pythonExe -m pip install --disable-pip-version-check --upgrade -r (Join-Path $projectDir "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies." }

& $pythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed." }

& $pythonExe -m PyInstaller --noconfirm --clean (Join-Path $projectDir "HFDownloader.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

if ($SkipInstaller) {
    Write-Host "Ready: dist\HF Downloader\HF Downloader.exe" -ForegroundColor Green
    exit 0
}

$programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
$programFiles64 = [Environment]::GetFolderPath("ProgramFiles")
$compilerCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $programFilesX86 "Inno Setup 6\ISCC.exe"),
    (Join-Path $programFiles64 "Inno Setup 6\ISCC.exe")
)
$iscc = $compilerCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 was not found. Install it or use .\build.ps1 -SkipInstaller." }

& $iscc (Join-Path $projectDir "installer\HFDownloader.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
Write-Host "Ready: release\HF-Downloader-Setup-1.1.1.exe" -ForegroundColor Green
