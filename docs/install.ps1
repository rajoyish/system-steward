<#
.SYNOPSIS
    System Steward installer for Windows (PowerShell 5.1 or 7).

.DESCRIPTION
    irm https://rajoyish.github.io/system-steward/install.ps1 | iex

    Installs the latest self-contained release (bundled Python, nothing else required) into
    %LOCALAPPDATA%\Programs\SystemSteward and adds it to your user PATH. No administrator rights.

    Uninstall:
    & ([scriptblock]::Create((irm https://rajoyish.github.io/system-steward/install.ps1))) -Uninstall

    Environment overrides: SYSTEM_STEWARD_VERSION (e.g. 3.0.0), SYSTEM_STEWARD_HOME.
#>
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = "rajoyish/system-steward"
$InstallDir = if ($env:SYSTEM_STEWARD_HOME) { $env:SYSTEM_STEWARD_HOME } else { Join-Path $env:LOCALAPPDATA "Programs\SystemSteward" }

function Set-UserPath([string]$Dir, [bool]$Present) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @(($current -split ';') | Where-Object { $_ -and ($_.TrimEnd('\') -ne $Dir.TrimEnd('\')) })
    if ($Present) { $parts += $Dir }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ';'), "User")
    $session = @(($env:Path -split ';') | Where-Object { $_ -and ($_.TrimEnd('\') -ne $Dir.TrimEnd('\')) })
    if ($Present) { $session += $Dir }
    $env:Path = $session -join ';'
}

if ($Uninstall) {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Set-UserPath $InstallDir $false
    Write-Host "System Steward removed."
    return
}

if (-not [Environment]::Is64BitOperatingSystem) { throw "System Steward requires 64-bit Windows." }

$Version = $env:SYSTEM_STEWARD_VERSION
if (-not $Version) {
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "system-steward-installer" }
    $Version = $release.tag_name
}
$Version = $Version.TrimStart('v')
$Archive = "system-steward-$Version-windows-x64.zip"
$Base = "https://github.com/$Repo/releases/download/v$Version"

$Temp = Join-Path ([IO.Path]::GetTempPath()) ("system-steward-" + [Guid]::NewGuid())
New-Item -ItemType Directory $Temp | Out-Null
try {
    Write-Host "==> Downloading System Steward $Version"
    Invoke-WebRequest "$Base/$Archive" -OutFile (Join-Path $Temp $Archive) -UseBasicParsing
    $sums = (Invoke-WebRequest "$Base/checksums.txt" -UseBasicParsing).Content
    $expected = ($sums -split "`n" | Where-Object { $_ -match "\s\*?$([regex]::Escape($Archive))\s*$" } | Select-Object -First 1) -replace '\s.*$', ''
    $actual = (Get-FileHash (Join-Path $Temp $Archive) -Algorithm SHA256).Hash
    if (-not $expected -or $expected.Trim().ToUpper() -ne $actual.ToUpper()) { throw "Checksum mismatch for $Archive" }

    Write-Host "==> Installing into $InstallDir"
    Expand-Archive (Join-Path $Temp $Archive) -DestinationPath $Temp
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    New-Item -ItemType Directory -Force (Split-Path $InstallDir) | Out-Null
    Move-Item (Join-Path $Temp "system-steward") $InstallDir
} finally {
    Remove-Item -Recurse -Force $Temp -ErrorAction SilentlyContinue
}

Set-UserPath $InstallDir $true
$reported = & (Join-Path $InstallDir "system-steward.cmd") --version
if ($LASTEXITCODE) { throw "The installed launcher did not start." }
Write-Host "==> Installed $reported"
Write-Host "Run: system-steward   (open a new terminal if the command is not found)"
