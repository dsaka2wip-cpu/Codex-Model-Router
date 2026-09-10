[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Start-AdaptiveCodex.ps1') -BypassRouter
