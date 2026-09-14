function Resolve-CodexAppPath {
    param(
        [string]$Preferred,
        [string]$RuntimeRegistry = (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\chrome-native-hosts-v2.json')
    )

    if (-not [string]::IsNullOrWhiteSpace($Preferred) -and (Test-Path -LiteralPath $Preferred -PathType Leaf)) {
        return [IO.Path]::GetFullPath($Preferred)
    }

    if (Test-Path -LiteralPath $RuntimeRegistry -PathType Leaf) {
        try {
            $registry = Get-Content -Raw -LiteralPath $RuntimeRegistry | ConvertFrom-Json
            foreach ($entry in ($registry.entries | Sort-Object updatedAt -Descending)) {
                if ($entry.paths.resourcesPath) {
                    $candidate = Join-Path (Split-Path $entry.paths.resourcesPath) 'ChatGPT.exe'
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                        return [IO.Path]::GetFullPath($candidate)
                    }
                }
            }
        } catch { }
    }

    foreach ($process in (Get-Process -Name ChatGPT -ErrorAction SilentlyContinue)) {
        try {
            if ($process.Path -and (Test-Path -LiteralPath $process.Path -PathType Leaf)) {
                return [IO.Path]::GetFullPath($process.Path)
            }
        } catch { }
    }

    foreach ($package in (Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue | Sort-Object Version -Descending)) {
        $candidate = Join-Path $package.InstallLocation 'app\ChatGPT.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }

    throw "Codex app is missing and the current installation could not be found. Saved path: $Preferred"
}

function Resolve-CodexCliPath {
    param(
        [string]$Preferred,
        [string]$BinDirectory = (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin')
    )

    if (-not [string]::IsNullOrWhiteSpace($Preferred) -and (Test-Path -LiteralPath $Preferred -PathType Leaf)) {
        return [IO.Path]::GetFullPath($Preferred)
    }

    $candidate = Get-ChildItem -LiteralPath $BinDirectory -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName 'codex.exe') -ErrorAction SilentlyContinue } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($candidate) { return $candidate.FullName }

    $pathCommand = Get-Command 'codex.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pathCommand -and $pathCommand.Source -and (Test-Path -LiteralPath $pathCommand.Source -PathType Leaf)) {
        return [IO.Path]::GetFullPath($pathCommand.Source)
    }

    throw "Codex CLI is missing and the current installation could not be found. Saved path: $Preferred"
}

function Test-PythonRuntimePath {
    param([string]$Candidate)

    if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    try {
        & $Candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-PythonRuntimePath {
    param(
        [string]$Preferred,
        [string]$Bundled = (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )

    if (-not [string]::IsNullOrWhiteSpace($Preferred)) {
        if (Test-PythonRuntimePath $Preferred) { return [IO.Path]::GetFullPath($Preferred) }
        throw "Python 3.8 or newer could not be started from the requested path: $Preferred"
    }

    $candidates = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($Bundled)) { $candidates.Add($Bundled) }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) { $candidates.Add($command.Source) }
    }
    $py = Get-Command 'py.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py -and $py.Source) {
        try {
            $reported = @(& $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null) | Select-Object -Last 1
            if ($LASTEXITCODE -eq 0 -and $reported) { $candidates.Add([string]$reported) }
        } catch { }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-PythonRuntimePath $candidate) { return [IO.Path]::GetFullPath($candidate) }
    }
    throw 'Python 3.8 or newer is required. Install Python, then run Start Adaptive Codex.cmd again.'
}

function Resolve-CSharpCompilerPath {
    param([string]$Preferred)

    $candidates = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($Preferred)) {
        $candidates.Add($Preferred)
    } else {
        $windows = [Environment]::GetFolderPath('Windows')
        $candidates.Add((Join-Path $windows 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'))
        $candidates.Add((Join-Path $windows 'Microsoft.NET\Framework\v4.0.30319\csc.exe'))
        $command = Get-Command 'csc.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and $command.Source) { $candidates.Add($command.Source) }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [IO.Path]::GetFullPath($candidate) }
    }
    throw 'A C# compiler is required to prepare the native launcher. Enable Windows .NET Framework 4.x, then run Start Adaptive Codex.cmd again.'
}
