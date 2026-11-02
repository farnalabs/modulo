#!/usr/bin/env pwsh
<#>
.SYNOPSIS
    Build the Modulo Windows native launcher (modulo.exe) from C source.

.DESCRIPTION
    Compiles scripts/windows/modulo-launcher.c using MSVC (cl.exe) or clang-cl
    into a native Windows executable. The launcher is a minimal bootstrap that
    sets up the environment and execs the bundled Python runtime.

    Requires Visual Studio Developer Command Prompt environment (vcvarsall.bat
    must have been run) or clang-cl in PATH.

.OUTPUTS
    modulo.exe - placed in the current directory (intended to be copied to bundle_root/bin/)

.EXAMPLE
    # From repo root in a Developer Command Prompt:
    pwsh scripts/windows/build-launcher.ps1

    # With clang-cl:
    pwsh scripts/windows/build-launcher.ps1 -Compiler clang-cl
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidateSet('msvc', 'clang-cl')]
    [string]$Compiler = 'msvc',

    [Parameter(Mandatory = $false)]
    [string]$OutputPath = 'modulo.exe',

    [Parameter(Mandatory = $false)]
    [switch]$Debug
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$sourceFile = Join-Path $PSScriptRoot 'modulo-launcher.c'
if (-not (Test-Path -LiteralPath $sourceFile)) {
    throw "Source file not found: $sourceFile"
}

# Determine compiler and flags
$compilerExe = if ($Compiler -eq 'msvc') { 'cl.exe' } else { 'clang-cl.exe' }
$commonFlags = @('/nologo', '/Wall', '/WX', '/wd4201', '/wd4214', '/wd4115', '/wd4514')
if ($Debug) {
    $optFlags = @('/Od', '/Zi', '/MDd')
} else {
    $optFlags = @('/O2', '/MD')
}
$outputFlag = "/Fe:$OutputPath"

# Verify compiler is available
$compilerPath = (Get-Command $compilerExe -ErrorAction SilentlyContinue).Source
if (-not $compilerPath) {
    throw "Compiler '$compilerExe' not found in PATH. Ensure you are in a Visual Studio Developer Command Prompt or have clang-cl installed."
}

Write-Host "Building Modulo Windows launcher..."
Write-Host "  Compiler: $compilerExe ($compilerPath)"
Write-Host "  Source:   $sourceFile"
Write-Host "  Output:   $OutputPath"
Write-Host "  Flags:    $commonFlags $optFlags $outputFlag"

# Build command
$buildArgs = @($commonFlags) + @($optFlags) + @($outputFlag, $sourceFile)
Write-Host "Executing: $compilerExe $($buildArgs -join ' ')"

$exitCode = & $compilerExe @buildArgs
if ($exitCode -ne 0) {
    throw "Compilation failed with exit code $exitCode"
}

if (-not (Test-Path -LiteralPath $OutputPath)) {
    throw "Output file not created: $OutputPath"
}

# Verify the executable runs and shows help/version
Write-Host "Verifying launcher executable..."
$verifyOutput = & $OutputPath --help 2>&1
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
    # Exit code 1 is expected for --help on a launcher that expects subcommands
    # Exit code 0 means it ran successfully (even if it shows usage)
    Write-Warning "Launcher verification returned exit code $LASTEXITCODE"
    Write-Host "Output: $verifyOutput"
}

Write-Host "Build successful: $OutputPath"
Write-Host "Copy this to bundle_root/bin/modulo.exe for the single-install bundle."
