$ErrorActionPreference = 'Stop'
$startScript = Join-Path $PSScriptRoot 'Start-AdaptiveCodex.ps1'
. $startScript -LoadFunctionsOnly

function Assert-ThrowsLike([scriptblock]$Action, [string]$Pattern) {
    $failure = $null
    try { & $Action } catch { $failure = $_ }
    if ($null -eq $failure) { throw "Expected a failure matching: $Pattern" }
    if ($failure.Exception.Message -notlike $Pattern) { throw $failure }
}

$powershell = (Get-Process -Id $PID).Path
Invoke-AdaptiveCodexLauncherPreflight -Command $powershell -Arguments '-NoProfile -NonInteractive -Command "exit 0"' -TimeoutMilliseconds 1000
Assert-ThrowsLike { Invoke-AdaptiveCodexLauncherPreflight -Command $powershell -Arguments '-NoProfile -NonInteractive -Command "exit 7"' -TimeoutMilliseconds 1000 } '*exited with code 7*'
Assert-ThrowsLike { Invoke-AdaptiveCodexLauncherPreflight -Command $powershell -Arguments '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 2"' -TimeoutMilliseconds 100 } '*timed out*'
& $powershell -NoProfile -NonInteractive -File $startScript -BypassRouter -CheckOnly
if ($LASTEXITCODE -ne 0) { throw "Bypass check failed with exit code $LASTEXITCODE." }

Write-Host 'Start preflight checks passed: success, failure, timeout, bypass.'
