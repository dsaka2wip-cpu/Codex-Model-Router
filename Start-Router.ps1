param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$routerRoot = $PSScriptRoot
$routerState = Join-Path $routerRoot 'state'
$routerRuntime = Join-Path $routerState 'runtime.json'

function Get-RouterUrl {
    if (-not (Test-Path -LiteralPath $routerRuntime)) { return $null }
    try {
        $routerInfo = Get-Content -Raw -LiteralPath $routerRuntime | ConvertFrom-Json
        $routerUri = [Uri]$routerInfo.url
        if ($routerUri.Scheme -ne 'http' -or $routerUri.Host -ne '127.0.0.1') { return $null }
        $routerToken = [Uri]::UnescapeDataString(($routerUri.Fragment -replace '^#token=', ''))
        $routerHeaders = @{'X-Router-Token'=$routerToken}
        $null = Invoke-RestMethod -Uri ($routerUri.GetLeftPart([UriPartial]::Authority) + '/api/state') -Headers $routerHeaders -TimeoutSec 2
        return $routerInfo.url
    } catch { return $null }
}

$routerUrl = Get-RouterUrl
if (-not $routerUrl) {
    $routerPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $routerPython)) {
        $routerPythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $routerPythonCommand) { throw 'Python 3 is required. Install Python or run main_router.py with an existing interpreter.' }
        $routerPython = $routerPythonCommand.Source
    }
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        $routerCodexRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
        $routerCodex = Get-ChildItem -LiteralPath $routerCodexRoot -Filter codex.exe -Recurse -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $routerCodex) { throw 'Codex CLI is required. Install it and run codex login first.' }
        $env:PATH = $routerCodex.DirectoryName + [IO.Path]::PathSeparator + $env:PATH
    }
    $null = New-Item -ItemType Directory -Force -Path $routerState
    $routerArguments = @('-u', ('"{0}"' -f (Join-Path $routerRoot 'main_router.py')))
    $routerProcess = Start-Process -FilePath $routerPython -ArgumentList $routerArguments -WorkingDirectory $routerRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $routerState 'stdout.log') -RedirectStandardError (Join-Path $routerState 'stderr.log')
    $routerDeadline = (Get-Date).AddSeconds(35)
    do {
        Start-Sleep -Milliseconds 400
        $routerUrl = Get-RouterUrl
        $routerProcess.Refresh()
        if ($routerProcess.HasExited -and -not $routerUrl) {
            throw ('Router could not start. See ' + (Join-Path $routerState 'stderr.log'))
        }
    } while (-not $routerUrl -and (Get-Date) -lt $routerDeadline)
    if (-not $routerUrl) { throw ('Router startup is still pending. Check ' + (Join-Path $routerState 'stderr.log')) }
}
if (-not $NoBrowser) { Start-Process $routerUrl }
Write-Output 'Codex Model Router is ready on localhost. No model call until a prompt is sent.'
