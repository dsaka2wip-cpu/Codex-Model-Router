[CmdletBinding()]
param(
    [string]$Python = (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    [string]$RealCodex = (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin\fd4c151a749f3ab4\codex.exe'),
    [string]$App = (Join-Path $env:ProgramFiles 'WindowsApps\OpenAI.Codex_26.903.8094.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe')
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
$state = Join-Path $root 'state'
$bin = Join-Path $state 'bin'
$source = Join-Path $root 'NativeRouterLauncher.cs'
$output = Join-Path $bin 'AdaptiveCodexRouter.exe'
$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'

foreach ($path in @($source, $python, $realCodex, $app, $compiler)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file is missing: $path" }
}
$prefix = $root.TrimEnd('\') + '\'
foreach ($path in @($state, $bin, $output)) {
    $full = [IO.Path]::GetFullPath($path)
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Build target escapes project root: $full" }
}

New-Item -ItemType Directory -Path $bin -Force | Out-Null
& $compiler /nologo /target:winexe /optimize+ /out:$output /reference:System.Web.Extensions.dll $source
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $output -PathType Leaf)) { throw 'Native launcher compilation failed.' }

$existingMetadata = Join-Path $state 'native-runtime.json'
$originalCli = $env:CODEX_CLI_PATH
if (Test-Path -LiteralPath $existingMetadata -PathType Leaf) {
    $saved = Get-Content -Raw -LiteralPath $existingMetadata | ConvertFrom-Json
    $originalCli = $saved.original_codex_cli_path
}
$metadata = [ordered]@{
    root = $root
    python = $python
    real_codex = $realCodex
    launcher = $output
    app_exe = $app
    original_codex_cli_path = if ([string]::IsNullOrWhiteSpace($originalCli)) { $null } else { $originalCli }
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $state 'native-runtime.json') -Encoding UTF8
Write-Host "Built $output"
