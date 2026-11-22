<#
.SYNOPSIS
    Keep Awake background launcher.
    Runs in the background even if the terminal closes.
    Turns off automatically when the computer turns off.
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Status,
    [switch]$Display,
    [int]$Duration,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$Launcher = Join-Path $PSScriptRoot "system-steward.ps1"

if ($Stop) {
    & $Launcher -Stop @ScriptArgs
} elseif ($Status) {
    & $Launcher -Status @ScriptArgs
} else {
    $callArgs = @{ Awake = $true }
    if ($Display) { $callArgs["Display"] = $true }
    if ($Duration -gt 0) { $callArgs["Duration"] = $Duration }
    & $Launcher @callArgs @ScriptArgs
}
exit $LASTEXITCODE
