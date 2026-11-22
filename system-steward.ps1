<#
.SYNOPSIS
    Launcher for System Steward on Windows using PowerShell 7.
#>
[CmdletBinding()]
param(
    [switch]$Awake,
    [switch]$KeepAwake,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Display,
    [int]$Duration,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ProjectDir = $PSScriptRoot
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $VenvPython) {
    $PythonExe = $VenvPython
} else {
    $candidates = @(
        "$env:USERPROFILE\scoop\apps\python\current\python.exe",
        "$env:USERPROFILE\scoop\shims\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\*\python.exe"
    )
    foreach ($cand in $candidates) {
        $resolved = Resolve-Path $cand -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($resolved -and (Test-Path -LiteralPath $resolved.Path)) {
            $PythonExe = $resolved.Path
            break
        }
    }
    if (-not $PythonExe) {
        $allPy = Get-Command python, python3 -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch "WindowsApps" }
        if ($allPy) {
            $PythonExe = ($allPy | Select-Object -First 1).Source
        }
    }
    if (-not $PythonExe) {
        Write-Error "Python executable not found. Please run .\setup.ps1 first."
        exit 1
    }
}

$MainScript = Join-Path $ProjectDir "system_steward.py"
$ForwardArgs = @()
if ($Awake -or $KeepAwake) {
    $ForwardArgs += "--keep-awake"
    if ($Display) { $ForwardArgs += "--display" }
    if ($Duration -gt 0) { $ForwardArgs += @("-t", "$Duration") }
} elseif ($Stop) {
    $ForwardArgs += "--stop-awake"
} elseif ($Status) {
    $ForwardArgs += "--status-awake"
}
if ($ScriptArgs) {
    $ForwardArgs += $ScriptArgs
}

& $PythonExe $MainScript @ForwardArgs
exit $LASTEXITCODE
