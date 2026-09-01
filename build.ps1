param([switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
Set-Location -LiteralPath $projectDir
$version = (Get-Content -LiteralPath (Join-Path $projectDir "VERSION") -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "VERSION does not contain a valid semantic version: $version"
}

if (-not (Test-Path -LiteralPath $pythonExe)) {
    $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $systemPython) { $systemPython = Get-Command py.exe -ErrorAction SilentlyContinue }
    if (-not $systemPython) { throw "Python 3.10 or newer was not found." }
    if ($systemPython.Name -eq "py.exe") {
        & $systemPython.Source -3 -m venv (Join-Path $projectDir ".venv")
    } else {
        & $systemPython.Source -m venv (Join-Path $projectDir ".venv")
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
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

& $iscc "/DAppVersion=$version" (Join-Path $projectDir "installer\HFDownloader.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
$installerName = "HF-Downloader-Setup-$version.exe"
$installerPath = Join-Path $projectDir "release\$installerName"
if (-not (Test-Path -LiteralPath $installerPath)) { throw "Installer was not created: $installerPath" }
$checksum = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = "$installerPath.sha256"
[IO.File]::WriteAllText($checksumPath, "$checksum  $installerName`r`n", [Text.UTF8Encoding]::new($false))
Write-Host "Ready: release\$installerName" -ForegroundColor Green
Write-Host "SHA-256: release\$installerName.sha256" -ForegroundColor Green
