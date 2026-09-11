[CmdletBinding()]
param(
    [switch]$BypassRouter,
    [switch]$CheckOnly,
    [switch]$WaitForExit,
    [switch]$LoadFunctionsOnly
)

function Test-AdaptiveCodexLauncher {
    param(
        [Parameter(Mandatory)][string]$Command,
        [string]$Arguments = '--version',
        [int]$TimeoutMilliseconds = 10000
    )

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Command
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $cleanEnvironment = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $start.EnvironmentVariables.GetEnumerator()) { $cleanEnvironment[$entry.Key] = $entry.Value }
    $start.EnvironmentVariables.Clear()
    foreach ($entry in $cleanEnvironment.GetEnumerator()) { $start.EnvironmentVariables[$entry.Key] = $entry.Value }
    try {
        $process = [Diagnostics.Process]::Start($start)
    } catch {
        throw "could not start the launcher: $($_.Exception.Message)"
    }
    if ($null -eq $process) { throw 'could not start the launcher.' }
    try {
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            try { $process.Kill() } catch { }
            $process.WaitForExit(1000) | Out-Null
            throw "timed out after $TimeoutMilliseconds ms."
        }
        if ($process.ExitCode -ne 0) { throw "exited with code $($process.ExitCode)." }
    } finally {
        $process.Dispose()
    }
}

function Invoke-AdaptiveCodexLauncherPreflight {
    param(
        [Parameter(Mandatory)][string]$Command,
        [switch]$BypassRouter,
        [string]$Arguments = '--version',
        [int]$TimeoutMilliseconds = 10000
    )

    if ($BypassRouter) { return }
    Test-AdaptiveCodexLauncher -Command $Command -Arguments $Arguments -TimeoutMilliseconds $TimeoutMilliseconds
}

if ($LoadFunctionsOnly) { return }

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
$metadataPath = Join-Path $root 'state\native-runtime.json'
if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { throw 'Run Build-NativeRouter.ps1 first.' }
$runtime = Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
if (-not [string]::Equals([IO.Path]::GetFullPath($runtime.root), $root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime metadata belongs to a different project path.' }
if (-not (Test-Path -LiteralPath $runtime.app_exe -PathType Leaf)) { throw "Codex app is missing: $($runtime.app_exe)" }
$savedBaseline = $BypassRouter -and -not [string]::IsNullOrWhiteSpace($runtime.original_codex_cli_path)
if (-not $BypassRouter) {
    $command = [IO.Path]::GetFullPath($runtime.launcher)
    $prefix = $root.TrimEnd('\') + '\'
    if (-not $command.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Launcher path escapes the project root.' }
    if (-not (Test-Path -LiteralPath $command -PathType Leaf)) { throw "Codex launcher is missing: $command" }
} elseif ($savedBaseline) {
    $command = [string]$runtime.original_codex_cli_path
    if ([IO.Path]::IsPathRooted($command) -and -not (Test-Path -LiteralPath $command)) { throw "Saved baseline Codex CLI is missing: $command" }
}

if (-not $BypassRouter) {
    try {
        Invoke-AdaptiveCodexLauncherPreflight -Command $command
    } catch {
        throw "Adaptive Codex launcher preflight failed; Codex was not started. $($_.Exception.Message)"
    }
}
if ($CheckOnly) {
    $message = if ($BypassRouter) { 'Bypass mode does not use the adaptive launcher; no launcher preflight is required.' } else { 'Adaptive Codex launcher preflight passed.' }
    Write-Host $message
    return
}
$running = Get-Process -Name ChatGPT -ErrorAction SilentlyContinue
if ($running -and -not $WaitForExit) { throw 'Codex is already running. Fully quit Codex, or run with -WaitForExit.' }
if ($running) {
    Write-Host 'Codex is still running. Quit it fully; Adaptive Codex will start automatically.'
    $deadline = [DateTime]::UtcNow.AddMinutes(5)
    while ((Get-Process -Name ChatGPT -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Name ChatGPT -ErrorAction SilentlyContinue) { throw 'Timed out waiting for Codex to exit.' }
}

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $runtime.app_exe
$start.UseShellExecute = $false
$cleanEnvironment = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) { $cleanEnvironment[$entry.Key] = $entry.Value }
$start.EnvironmentVariables.Clear()
foreach ($entry in $cleanEnvironment.GetEnumerator()) { $start.EnvironmentVariables[$entry.Key] = $entry.Value }
if ($BypassRouter -and -not $savedBaseline) {
    $start.EnvironmentVariables.Remove('CODEX_CLI_PATH') | Out-Null
} else {
    $start.EnvironmentVariables['CODEX_CLI_PATH'] = $command
}
$process = [Diagnostics.Process]::Start($start)
if ($null -eq $process) { throw 'Codex did not start.' }
$message = if ($BypassRouter) { 'Started Codex with its saved baseline launch environment.' } else { 'Started Codex with adaptive routing for this app process.' }
Write-Host $message
