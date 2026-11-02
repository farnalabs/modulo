#!/usr/bin/env pwsh
[CmdletBinding()]
param(
    [ValidateSet('msvc', 'clang-cl')]
    [string]$Compiler = 'msvc',
    [string]$OutputPath = 'modulo.exe',
    [switch]$Debug
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
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
