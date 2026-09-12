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

    throw "Codex CLI is missing and the current installation could not be found. Saved path: $Preferred"
}
