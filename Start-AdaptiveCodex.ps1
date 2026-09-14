[CmdletBinding()]
param(
    [switch]$BypassRouter,
    [switch]$ClassifierEval,
    [ValidateRange(1, 12)][int]$ClassifierEvalLimit = 6,
    [switch]$CheckOnly,
    [switch]$WaitForExit,
    [switch]$LoadFunctionsOnly
)

function Set-AdaptiveCodexProcessEnvironment {
    param([Parameter(Mandatory)][Diagnostics.ProcessStartInfo]$StartInfo)

    # cmd.exe can pass both Path and PATH. .NET Framework's first getter
    # then throws after allocating a partially populated dictionary.
    $clean = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) { $clean[$entry.Key] = $entry.Value }
    try {
        $environment = $StartInfo.get_EnvironmentVariables()
    } catch {
        if ($_.Exception.InnerException -isnot [ArgumentException]) { throw }
        $environment = $StartInfo.get_EnvironmentVariables()
    }
    $environment.Clear()
    foreach ($entry in $clean.GetEnumerator()) { $environment[$entry.Key] = $entry.Value }
}
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
    Set-AdaptiveCodexProcessEnvironment -StartInfo $start
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

. (Join-Path $PSScriptRoot 'Resolve-CodexRuntime.ps1')

if ($LoadFunctionsOnly) { return }

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
$statePath = Join-Path $root 'state'
$configPath = Join-Path $statePath 'adaptive-config.json'
$defaultConfigPath = Join-Path $root 'adaptive-config.default.json'
$metadataPath = Join-Path $root 'state\native-runtime.json'

New-Item -ItemType Directory -Path $statePath -Force | Out-Null
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $defaultConfigPath -PathType Leaf)) { throw "Default configuration is missing: $defaultConfigPath" }
    Copy-Item -LiteralPath $defaultConfigPath -Destination $configPath
    Write-Host 'Created the default adaptive routing configuration.'
}

$buildRequired = -not (Test-Path -LiteralPath $metadataPath -PathType Leaf)
if (-not $buildRequired) {
    try {
        $savedRuntime = Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
        $buildRequired = (-not [string]::Equals([IO.Path]::GetFullPath([string]$savedRuntime.root), $root, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath ([string]$savedRuntime.launcher) -PathType Leaf) -or
            -not (Test-Path -LiteralPath ([string]$savedRuntime.python) -PathType Leaf))
        if (-not $buildRequired) {
            $launcherSource = Get-Item -LiteralPath (Join-Path $root 'NativeRouterLauncher.cs')
            $launcherBinary = Get-Item -LiteralPath ([string]$savedRuntime.launcher)
            $buildRequired = $launcherSource.LastWriteTimeUtc -gt $launcherBinary.LastWriteTimeUtc
        }
    } catch {
        $buildRequired = $true
    }
}
if ($buildRequired) {
    Write-Host 'Preparing Adaptive Codex for first use...'
    & (Join-Path $root 'Build-NativeRouter.ps1')
}

$runtime = Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
if (-not [string]::Equals([IO.Path]::GetFullPath($runtime.root), $root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime metadata belongs to a different project path.' }
$app = Resolve-CodexAppPath -Preferred ([string]$runtime.app_exe)
$realCodex = Resolve-CodexCliPath -Preferred ([string]$runtime.real_codex)
if ($runtime.app_exe -ne $app -or $runtime.real_codex -ne $realCodex) {
    $runtime.app_exe = $app
    $runtime.real_codex = $realCodex
    $temporaryMetadata = "$metadataPath.tmp"
    $runtime | ConvertTo-Json | Set-Content -LiteralPath $temporaryMetadata -Encoding UTF8
    Move-Item -LiteralPath $temporaryMetadata -Destination $metadataPath -Force
}
$savedBaseline = $BypassRouter -and -not [string]::IsNullOrWhiteSpace($runtime.original_codex_cli_path)
if ($BypassRouter -and $ClassifierEval) { throw 'Classifier evaluation requires adaptive routing.' }
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
$start.FileName = $app
$start.UseShellExecute = $false
Set-AdaptiveCodexProcessEnvironment -StartInfo $start
if ($BypassRouter -and -not $savedBaseline) {
    $start.EnvironmentVariables.Remove('CODEX_CLI_PATH') | Out-Null
} else {
    $start.EnvironmentVariables['CODEX_CLI_PATH'] = $command
}
$start.EnvironmentVariables.Remove('CODEX_ROUTER_CLASSIFIER_EVAL') | Out-Null
if ($ClassifierEval) { $start.EnvironmentVariables['CODEX_ROUTER_CLASSIFIER_EVAL'] = [string]$ClassifierEvalLimit }
$process = [Diagnostics.Process]::Start($start)
if ($null -eq $process) { throw 'Codex did not start.' }
$message = if ($BypassRouter) { 'Started Codex with its saved baseline launch environment.' } elseif ($ClassifierEval) { 'Started Codex with adaptive routing and one bounded classifier evaluation.' } else { 'Started Codex with adaptive routing for this app process.' }
Write-Host $message
