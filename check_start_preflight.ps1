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
$fakeAppRoot = Join-Path ([IO.Path]::GetTempPath()) ("codex-router-app-" + [Guid]::NewGuid().ToString('N'))
$fakeApp = Join-Path $fakeAppRoot 'ChatGPT.exe'
$fakeRegistry = Join-Path $fakeAppRoot 'runtime.json'
$discoveredApp = Join-Path $fakeAppRoot 'installed\app\ChatGPT.exe'
$fakeCliRoot = Join-Path $fakeAppRoot 'bin'
$fakeCli = Join-Path $fakeCliRoot 'current\codex.exe'
New-Item -ItemType Directory -Path $fakeAppRoot | Out-Null
New-Item -ItemType File -Path $fakeApp | Out-Null
New-Item -ItemType Directory -Path (Split-Path $discoveredApp) | Out-Null
New-Item -ItemType File -Path $discoveredApp | Out-Null
@{ entries = @(@{ updatedAt = '2026-09-12T00:00:00Z'; paths = @{ resourcesPath = (Join-Path (Split-Path $discoveredApp) 'resources') } }) } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $fakeRegistry
New-Item -ItemType Directory -Path (Split-Path $fakeCli) | Out-Null
New-Item -ItemType File -Path $fakeCli | Out-Null
try {
    if ((Resolve-CodexAppPath -Preferred $fakeApp) -ne [IO.Path]::GetFullPath($fakeApp)) { throw 'Preferred Codex app path was not preserved.' }
    if ((Resolve-CodexAppPath -Preferred 'missing.exe' -RuntimeRegistry $fakeRegistry) -ne [IO.Path]::GetFullPath($discoveredApp)) { throw 'Current Codex app path was not discovered.' }
    if ((Resolve-CodexCliPath -Preferred 'missing.exe' -BinDirectory $fakeCliRoot) -ne [IO.Path]::GetFullPath($fakeCli)) { throw 'Current Codex CLI path was not discovered.' }
} finally {
    Remove-Item -LiteralPath $fakeAppRoot -Recurse -Force
}
Invoke-AdaptiveCodexLauncherPreflight -Command $powershell -Arguments '-NoProfile -NonInteractive -Command "exit 0"' -TimeoutMilliseconds 5000
Assert-ThrowsLike { Invoke-AdaptiveCodexLauncherPreflight -Command $powershell -Arguments '-NoProfile -NonInteractive -Command "exit 7"' -TimeoutMilliseconds 5000 } '*exited with code 7*'
Assert-ThrowsLike { Invoke-AdaptiveCodexLauncherPreflight -Command $powershell -Arguments '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 2"' -TimeoutMilliseconds 100 } '*timed out*'
& $powershell -NoProfile -NonInteractive -File $startScript -BypassRouter -CheckOnly
if ($LASTEXITCODE -ne 0) { throw "Bypass check failed with exit code $LASTEXITCODE." }

Write-Host 'Start preflight checks passed: app resolution, success, failure, timeout, bypass.'
