<#
.SYNOPSIS
    Builds the self-contained Windows release: a portable zip (Scoop, install.ps1) and a per-user
    Inno Setup installer (winget).

.DESCRIPTION
    Layout of both:  system-steward\
                       system-steward.cmd   launcher on PATH
                       python\              official embeddable CPython + pinned wheels in Lib\site-packages
                       app\                 System Steward source (from the release source archive)
    Users need no Python installation. The bundled python.exe also serves the app's helper
    processes (folder measurement and the Keep Awake helper), which is why the app is not frozen.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Version,
    [string]$PythonVersion = "3.13.9",
    [string]$OutDir = "dist"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Out = Join-Path $Root $OutDir
$Work = Join-Path ([IO.Path]::GetTempPath()) "system-steward-build"
$Stage = Join-Path $Work "system-steward"
Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Out, $Stage | Out-Null

$minor = ($PythonVersion -split '\.')[0..1] -join '.'
$runner = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])")
if ($runner -ne $minor) { throw "Build Python is $runner but the bundle uses $minor; install Python $minor first." }

Write-Host "==> Embeddable Python $PythonVersion"
$embed = Join-Path $Work "python-embed.zip"
Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $embed
Expand-Archive $embed -DestinationPath (Join-Path $Stage "python")
$tag = $minor -replace '\.', ''
# The ._pth file fixes sys.path for the embedded interpreter (it ignores PYTHONPATH and user site-packages).
Set-Content -Encoding ascii (Join-Path $Stage "python\python$tag._pth") -Value @(
    "python$tag.zip", ".", "Lib\site-packages", "..\app", "import site")

Write-Host "==> Pinned requirements"
& python -m pip install --disable-pip-version-check --no-deps --only-binary=:all: `
    --target (Join-Path $Stage "python\Lib\site-packages") -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE) { throw "pip install failed" }

Write-Host "==> Application"
& python (Join-Path $Root "packaging\release.py") source $Version $Work
if ($LASTEXITCODE) { throw "source archive failed" }
tar -xzf (Join-Path $Work "system-steward-$Version.tar.gz") -C $Work
Move-Item (Join-Path $Work "system-steward-$Version") (Join-Path $Stage "app")

# Exit code 75 means "update me": the app has written %TEMP%\system-steward-update.cmd and exited
# so Scoop / the winget installer can replace its files. The update runs from one parenthesised
# line because cmd parses that whole block up front; the update replaces this very launcher, so
# cmd must never read another line of it afterwards (exit /b runs before it would).
Set-Content -Encoding ascii (Join-Path $Stage "system-steward.cmd") -Value @(
    '@echo off',
    '"%~dp0python\python.exe" "%~dp0app\system_steward.py" %*',
    'if %ERRORLEVEL% equ 75 if exist "%TEMP%\system-steward-update.cmd" (call "%TEMP%\system-steward-update.cmd" & exit /b)',
    'exit /b %ERRORLEVEL%')

Write-Host "==> Smoke test"
$reported = & (Join-Path $Stage "system-steward.cmd") --version
if ($LASTEXITCODE -or "$reported" -notmatch [regex]::Escape($Version)) { throw "launcher check failed: $reported" }
& (Join-Path $Stage "python\python.exe") -c "import ctypes, textual, rich, tui.app, windows_caffeinate"
if ($LASTEXITCODE) { throw "import check failed" }
Get-ChildItem $Stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Write-Host "==> Portable zip"
$zip = Join-Path $Out "system-steward-$Version-windows-x64.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $Stage -DestinationPath $zip

Write-Host "==> Installer"
$iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    choco install innosetup --yes --no-progress
    if ($LASTEXITCODE) { throw "Inno Setup install failed" }
}
& $iscc "/DAppVersion=$Version" "/DSourceDir=$Stage" "/DOutputDir=$Out" (Join-Path $PSScriptRoot "system-steward.iss")
if ($LASTEXITCODE) { throw "Inno Setup build failed" }
Get-ChildItem $Out
