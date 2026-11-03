#!/usr/bin/env pwsh
param(
    [ValidateSet('msvc', 'clang-cl')]
    [string]$Compiler = 'msvc',
    [string]$OutputPath = 'modulo.exe',
    [switch]$Debug
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Copy-DirectoryContents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$Source,
        [Parameter(Mandatory)] [string]$Destination
    )

    # Merge the source's children into the destination. Copying the source
    # directory itself into an existing directory nests it (src/src), so every
    # payload is staged by piping its children instead. Re-staging into an
    # existing tree overwrites files in place and is therefore idempotent.
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $sourcePath = (Get-Item -LiteralPath $Source -Force).FullName
    $destinationPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Destination)
    $comparison = if ($separator -eq '\') { [System.StringComparison]::OrdinalIgnoreCase } else { [System.StringComparison]::Ordinal }
    $sourceKey = $sourcePath.TrimEnd([char[]]@('\', '/'))
    $destinationKey = $destinationPath.TrimEnd([char[]]@('\', '/'))
    if ([string]::Equals($sourceKey, $destinationKey, $comparison)) {
        # Source already is the destination (e.g. re-staging in place); nothing to copy.
        return
    }
    New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null
    Get-ChildItem -LiteralPath $sourcePath -Force | Copy-Item -Destination $destinationPath -Recurse -Force
}

function Invoke-WindowsBundleStaging {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$StagePath,
        [Parameter(Mandatory)] [string]$PythonRoot,
        [Parameter(Mandatory)] [string]$BackendSource,
        [Parameter(Mandatory)] [string]$SitePackagesSource,
        [Parameter(Mandatory)] [string]$FrontendDistSource,
        [Parameter(Mandatory)] [string]$LauncherPath,
        # Release version recorded in modulo-runtime.json; omitted when unknown.
        [string]$Version,
        [switch]$PreserveExisting
    )

    $ErrorActionPreference = 'Stop'
    if (-not $PreserveExisting -and (Test-Path -LiteralPath $StagePath)) { Remove-Item -LiteralPath $StagePath -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Join-Path $StagePath 'runtime/python'), (Join-Path $StagePath 'runtime/python/Lib/site-packages'), (Join-Path $StagePath 'runtime/bin'), (Join-Path $StagePath 'backend'), (Join-Path $StagePath 'frontend-dist'), (Join-Path $StagePath 'bin') | Out-Null
    foreach ($required in @(
        (Join-Path $PythonRoot 'python.exe'),
        (Join-Path $PythonRoot 'python312.dll'),
        $BackendSource,
        $SitePackagesSource,
        $FrontendDistSource,
        $LauncherPath
    )) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Bundle staging input missing: $required" }
    }
    Copy-DirectoryContents -Source $PythonRoot -Destination (Join-Path $StagePath 'runtime/python')
    Copy-DirectoryContents -Source $BackendSource -Destination (Join-Path $StagePath 'backend/src')
    Copy-DirectoryContents -Source $SitePackagesSource -Destination (Join-Path $StagePath 'runtime/python/Lib/site-packages')
    Copy-DirectoryContents -Source $FrontendDistSource -Destination (Join-Path $StagePath 'frontend-dist')
    Copy-Item -LiteralPath $LauncherPath -Destination (Join-Path $StagePath 'bin/modulo.exe') -Force
    # The launcher invokes runtime\bin\python3.exe; provide both interpreter entry points.
    Copy-Item -LiteralPath (Join-Path $StagePath 'runtime/python/python.exe') -Destination (Join-Path $StagePath 'runtime/bin/python3.exe') -Force
    Copy-Item -LiteralPath (Join-Path $StagePath 'runtime/python/python312.dll') -Destination (Join-Path $StagePath 'runtime/bin/python312.dll') -Force
    foreach ($required in @(
        (Join-Path $StagePath 'runtime/python/python.exe'),
        (Join-Path $StagePath 'runtime/python/python312.dll'),
        (Join-Path $StagePath 'runtime/bin/python3.exe'),
        (Join-Path $StagePath 'runtime/bin/python312.dll'),
        (Join-Path $StagePath 'backend/src'),
        (Join-Path $StagePath 'runtime/python/Lib/site-packages'),
        (Join-Path $StagePath 'frontend-dist'),
        (Join-Path $StagePath 'bin/modulo.exe')
    )) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Bundle staging output missing: $required" }
    }
    # deploy/windows/modulo.iss requires this marker at the bundle root and
    # nothing else writes it. "contract" is the field scripts/test-windows-installer.ps1
    # pins; the .iss only checks that the file exists.
    $marker = [ordered]@{ contract = 'native-windows-v1'; arch = 'amd64' }
    if ($Version) { $marker['version'] = $Version }
    $markerPath = Join-Path $StagePath 'modulo-runtime.json'
    [System.IO.File]::WriteAllText($markerPath, (($marker | ConvertTo-Json -Compress) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) { throw "Bundle staging output missing: $markerPath" }
    return $StagePath
}

function Invoke-WindowsLauncherBuild {
    $sourceFile = Join-Path $PSScriptRoot 'modulo-launcher.c'
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "Source file not found: $sourceFile" }
    $compilerExe = if ($Compiler -eq 'msvc') { 'cl.exe' } else { 'clang-cl.exe' }
    $compiler = Get-Command $compilerExe -ErrorAction SilentlyContinue
    if ($null -eq $compiler) { throw "Compiler '$compilerExe' not found in PATH." }
    # /wd4711: informational auto-inline note; /wd5045: informational Spectre
    # mitigation hint. Both are emitted under /Wall and would fail /WX.
    $flags = @('/nologo', '/Wall', '/WX', '/wd4201', '/wd4214', '/wd4115', '/wd4514', '/wd4711', '/wd5045')
    $flags += if ($Debug) { @('/Od', '/Zi', '/MDd') } else { @('/O2', '/MD') }
    $buildArgs = $flags + @("/Fe:$OutputPath", $sourceFile)
    & $compiler.Source @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Compilation failed with exit code $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) { throw "Output file not created: $OutputPath" }
    # The message goes to the information stream so a caller's pipeline output is
    # untouched; -InformationAction Continue keeps it visible in the CI log,
    # where the default SilentlyContinue preference would swallow it.
    Write-Information -MessageData "Built $OutputPath with $compilerExe" -InformationAction Continue
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-WindowsLauncherBuild
}
