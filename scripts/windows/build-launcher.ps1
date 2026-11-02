#!/usr/bin/env pwsh
param(
    [ValidateSet('msvc', 'clang-cl')]
    [string]$Compiler = 'msvc',
    [string]$OutputPath = 'modulo.exe',
    [switch]$Debug
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-WindowsBundleStaging {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string]$StagePath,
        [Parameter(Mandatory)] [string]$PythonRoot,
        [Parameter(Mandatory)] [string]$BackendSource,
        [Parameter(Mandatory)] [string]$SitePackagesSource,
        [Parameter(Mandatory)] [string]$FrontendDistSource,
        [Parameter(Mandatory)] [string]$LauncherPath,
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
    Get-ChildItem -LiteralPath $PythonRoot -Force | Copy-Item -Destination (Join-Path $StagePath 'runtime/python') -Recurse -Force
    Copy-Item -LiteralPath $BackendSource -Destination (Join-Path $StagePath 'backend/src') -Recurse -Force
    Get-ChildItem -LiteralPath $SitePackagesSource -Force | Copy-Item -Destination (Join-Path $StagePath 'runtime/python/Lib/site-packages') -Recurse -Force
    Get-ChildItem -LiteralPath $FrontendDistSource -Force | Copy-Item -Destination (Join-Path $StagePath 'frontend-dist') -Recurse -Force
    Copy-Item -LiteralPath $LauncherPath -Destination (Join-Path $StagePath 'bin/modulo.exe') -Force
    foreach ($required in @(
        (Join-Path $StagePath 'runtime/python/python.exe'),
        (Join-Path $StagePath 'runtime/python/python312.dll'),
        (Join-Path $StagePath 'backend/src'),
        (Join-Path $StagePath 'runtime/python/Lib/site-packages'),
        (Join-Path $StagePath 'frontend-dist'),
        (Join-Path $StagePath 'bin/modulo.exe')
    )) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Bundle staging output missing: $required" }
    }
    return $StagePath
}

function Invoke-WindowsLauncherBuild {
    $sourceFile = Join-Path $PSScriptRoot 'modulo-launcher.c'
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "Source file not found: $sourceFile" }
    $compilerExe = if ($Compiler -eq 'msvc') { 'cl.exe' } else { 'clang-cl.exe' }
    $compiler = Get-Command $compilerExe -ErrorAction SilentlyContinue
    if ($null -eq $compiler) { throw "Compiler '$compilerExe' not found in PATH." }
    $flags = @('/nologo', '/Wall', '/WX', '/wd4201', '/wd4214', '/wd4115', '/wd4514')
    $flags += if ($Debug) { @('/Od', '/Zi', '/MDd') } else { @('/O2', '/MD') }
    $buildArgs = $flags + @("/Fe:$OutputPath", $sourceFile)
    & $compiler.Source @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "Compilation failed with exit code $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) { throw "Output file not created: $OutputPath" }
    Write-Host "Built $OutputPath with $compilerExe"
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-WindowsLauncherBuild
}
