<#
.SYNOPSIS
    SystemSteward launcher alias for Windows (PascalCase).
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$Launcher = Join-Path $PSScriptRoot "system-steward.ps1"
& $Launcher @ScriptArgs
exit $LASTEXITCODE
