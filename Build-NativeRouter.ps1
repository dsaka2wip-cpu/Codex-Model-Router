[CmdletBinding()]
param(
    [string]$Python,
    [string]$RealCodex,
    [string]$App,
    [string]$Compiler
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
. (Join-Path $root 'Resolve-CodexRuntime.ps1')
$python = Resolve-PythonRuntimePath -Preferred $Python
$compiler = Resolve-CSharpCompilerPath -Preferred $Compiler
$app = Resolve-CodexAppPath -Preferred $App
$realCodex = Resolve-CodexCliPath -Preferred $RealCodex
$state = Join-Path $root 'state'
$bin = Join-Path $state 'bin'
$config = Join-Path $state 'adaptive-config.json'
$defaultConfig = Join-Path $root 'adaptive-config.default.json'
$source = Join-Path $root 'NativeRouterLauncher.cs'
$output = Join-Path $bin 'AdaptiveCodexRouter.exe'

foreach ($path in @($source, $defaultConfig, $python, $realCodex, $app, $compiler)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file is missing: $path" }
}
$prefix = $root.TrimEnd('\') + '\'
foreach ($path in @($state, $bin, $output)) {
    $full = [IO.Path]::GetFullPath($path)
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Build target escapes project root: $full" }
}

New-Item -ItemType Directory -Path $bin -Force | Out-Null
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    Copy-Item -LiteralPath $defaultConfig -Destination $config
    Write-Host "Created $config"
}
$temporaryOutput = Join-Path $bin ("AdaptiveCodexRouter-" + [Guid]::NewGuid().ToString('N') + '.exe')
try {
    & $compiler /nologo /target:winexe /optimize+ /out:$temporaryOutput /reference:System.Web.Extensions.dll $source
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporaryOutput -PathType Leaf)) { throw 'Native launcher compilation failed.' }
    Move-Item -LiteralPath $temporaryOutput -Destination $output -Force
} finally {
    Remove-Item -LiteralPath $temporaryOutput -Force -ErrorAction SilentlyContinue
}

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
$metadataPath = Join-Path $state 'native-runtime.json'
$temporaryMetadata = "$metadataPath.tmp"
try {
    $metadata | ConvertTo-Json | Set-Content -LiteralPath $temporaryMetadata -Encoding UTF8
    Move-Item -LiteralPath $temporaryMetadata -Destination $metadataPath -Force
} finally {
    Remove-Item -LiteralPath $temporaryMetadata -Force -ErrorAction SilentlyContinue
}
Write-Host "Built $output"
