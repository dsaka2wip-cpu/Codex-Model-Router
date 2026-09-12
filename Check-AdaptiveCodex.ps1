param([switch]$Json)

$ErrorActionPreference = 'Stop'

function Last-Event([object[]]$Events, [string]$Name) {
    return $Events | Where-Object event -eq $Name | Select-Object -Last 1
}

function Event-IsAfter($Left, $Right) {
    return $Left -and (-not $Right -or
        [DateTimeOffset]::Parse($Left.at).UtcDateTime -gt [DateTimeOffset]::Parse($Right.at).UtcDateTime)
}

$routerProcesses = @(Get-Process -Name 'AdaptiveCodexRouter' -ErrorAction SilentlyContinue)
$chatgptProcesses = @(Get-Process -Name 'ChatGPT' -ErrorAction SilentlyContinue)
$log = Get-ChildItem (Join-Path $PSScriptRoot 'state\adaptive-logs') -Filter 'adaptive-*.jsonl' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$events = @()
if ($log) {
    $lines = @(
        Get-Content -LiteralPath $log.FullName -TotalCount 50
        Get-Content -LiteralPath $log.FullName -Tail 500
    )
    $events = @($lines | ForEach-Object {
        try { $_ | ConvertFrom-Json } catch { }
    })
}

$bridge = Last-Event $events 'bridge_started'
$auth = Last-Event $events 'auth'
$route = Last-Event $events 'route'
$completed = Last-Event $events 'turn_completed'
$footer = Last-Event $events 'footer'
$idle = Last-Event $events 'idle_restored'
$bridgeAlive = $false
if ($bridge -and $bridge.pid) {
    $bridgeAlive = $null -ne (Get-Process -Id $bridge.pid -ErrorAction SilentlyContinue)
}

if ($routerProcesses.Count -gt 0) {
    $mode = 'adaptive'
    if (-not $bridge) { $phase = 'starting' }
    elseif (-not $bridgeAlive) { $phase = 'broken' }
    elseif (Event-IsAfter $route $completed) { $phase = 'working' }
    elseif ($idle -and -not (Event-IsAfter $route $idle)) { $phase = 'ready' }
    else { $phase = 'connected' }
} elseif ($chatgptProcesses.Count -gt 0) {
    $mode = 'native'
    $phase = 'router_bypassed'
} else {
    $mode = 'stopped'
    $phase = 'stopped'
}

$result = [ordered]@{
    mode = $mode
    phase = $phase
    router_processes = $routerProcesses.Count
    bridge_alive = $bridgeAlive
    chatgpt_processes = $chatgptProcesses.Count
    auth = if ($mode -eq 'adaptive' -and $auth) { $auth.auth } else { $null }
    last_route = if ($mode -eq 'adaptive' -and $route) {
        [ordered]@{ tier = $route.tier; model = $route.model; effort = $route.effort }
    } else { $null }
    last_turn = if ($mode -eq 'adaptive' -and $completed) { $completed.status } else { $null }
    saved_percent = if ($mode -eq 'adaptive' -and $footer) { $footer.saved_percent } else { $null }
    idle = if ($mode -eq 'adaptive' -and $idle) {
        [ordered]@{ model = $idle.model; effort = $idle.effort }
    } else { $null }
    last_event_at = if ($mode -eq 'adaptive' -and $events.Count) { $events[-1].at } else { $null }
}

if ($Json) {
    $result | ConvertTo-Json -Depth 3 -Compress
    exit 0
}

$modeLabel = @{ adaptive = 'ADAPTIVE ROUTER'; native = 'NATIVE CODEX (Router bypassed)'; stopped = 'STOPPED' }[$mode]
$phaseLabel = @{ ready = 'Ready'; working = 'Working'; connected = 'Connected'; starting = 'Starting'; broken = 'Bridge stopped'; router_bypassed = 'Router bypassed'; stopped = 'Stopped' }[$phase]
Write-Host "Codex mode : $modeLabel"
Write-Host "Status     : $phaseLabel"
if ($result.auth) { Write-Host "Auth       : $($result.auth)" }
if ($result.last_route) {
    Write-Host "Last route : $($result.last_route.tier): $($result.last_route.model) / $($result.last_route.effort)"
}
if ($result.last_turn) { Write-Host "Last turn  : $($result.last_turn)" }
if ($null -ne $result.saved_percent) { Write-Host "Saved est. : $($result.saved_percent)%" }
if ($result.idle) { Write-Host "Next turn  : $($result.idle.model) / $($result.idle.effort)" }
if ($routerProcesses.Count -gt 1) { Write-Warning "Multiple Router processes detected: $($routerProcesses.Count)" }
