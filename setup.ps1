<#
.SYNOPSIS
    Setup script for System Steward on Windows using PowerShell 7.
.DESCRIPTION
    Creates a local Python virtual environment (.venv) and installs
    pinned dependencies from requirements.txt.
#>
[CmdletBinding()]
param(
    [string]$PythonBin = $env:SYSTEM_STEWARD_PYTHON
)

$ErrorActionPreference = "Stop"

if (-not $PythonBin) {
    if ($env:MAC_STEWARD_PYTHON) { $PythonBin = $env:MAC_STEWARD_PYTHON }
    elseif ($env:STORAGE_PYTHON) { $PythonBin = $env:STORAGE_PYTHON }
    else {
        # Check Scoop Python first
        $candidates = @(
            "$env:USERPROFILE\scoop\apps\python\current\python.exe",
            "$env:USERPROFILE\scoop\shims\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\*\python.exe"
        )
        foreach ($cand in $candidates) {
            $resolved = Resolve-Path $cand -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($resolved -and (Test-Path -LiteralPath $resolved.Path)) {
                $PythonBin = $resolved.Path
                break
            }
        }
        if (-not $PythonBin) {
            $allPy = Get-Command python, python3 -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch "WindowsApps" }
            if ($allPy) {
                $PythonBin = ($allPy | Select-Object -First 1).Source
            }
        }
        if (-not $PythonBin) {
            throw "Python executable not found. Please install Python using: scoop install python"
        }
    }
}

$ProjectDir = $PSScriptRoot
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "Setting up System Steward virtual environment..." -ForegroundColor Cyan
Write-Host "Using Python: $PythonBin" -ForegroundColor Gray

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating virtual environment at $VenvDir..." -ForegroundColor Cyan
    & $PythonBin -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment."
    }
}

# Ensure pip is available
& $VenvPython -m pip --version >$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Bootstrapping pip via ensurepip..." -ForegroundColor Cyan
    & $VenvPython -m ensurepip --upgrade
}

# Install requirements
$WheelsDir = Join-Path $ProjectDir ".wheels"
$RequirementsFile = Join-Path $ProjectDir "requirements.txt"

if (Test-Path -LiteralPath $WheelsDir) {
    Write-Host "Installing dependencies from local wheels..." -ForegroundColor Cyan
    & $VenvPython -m pip install --no-index --find-links $WheelsDir -r $RequirementsFile
} else {
    Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Cyan
    & $VenvPython -m pip install -r $RequirementsFile
}

if ($LASTEXITCODE -ne 0) {
    throw "Failed to install dependencies."
}

Write-Host "`nReady. Run .\system-steward.ps1 to open System Steward." -ForegroundColor Green
