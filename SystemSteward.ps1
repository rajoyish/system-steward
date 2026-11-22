<#
.SYNOPSIS
    SystemSteward launcher alias for Windows (PascalCase).
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

$Launcher = Join-Path $PSScriptRoot "system-steward.ps1"
$callArgs = @{}
if ($Awake) { $callArgs["Awake"] = $true }
if ($KeepAwake) { $callArgs["KeepAwake"] = $true }
if ($Stop) { $callArgs["Stop"] = $true }
if ($Status) { $callArgs["Status"] = $true }
if ($Display) { $callArgs["Display"] = $true }
if ($Duration -gt 0) { $callArgs["Duration"] = $Duration }

& $Launcher @callArgs @ScriptArgs
exit $LASTEXITCODE
