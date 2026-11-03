#!/usr/bin/env pwsh
# Validate the Windows native-launcher command contract against a real binary.
# Kept separate from create-windows-launcher.ps1 so the dry-run release path
# (no native bundle yet) never executes a binary or writes a fake executable.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ExePath
)

$ErrorActionPreference = 'Stop'

if ([System.IO.Path]::GetExtension($ExePath) -ne '.exe') {
    throw "Windows launcher must be a native .exe path: $ExePath"
}
if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
    throw "Windows launcher binary not found at $ExePath"
}

Write-Host "Validating Windows launcher command contract for version $Version..."

$helpResult = & $ExePath --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher --help failed with exit code ${LASTEXITCODE}: $helpResult"
}

$requiredCommands = @('start', 'stop', 'status', 'doctor', 'support', 'version', 'env', 'logs')
$missingCommands = @()
foreach ($cmd in $requiredCommands) {
    $commandPattern = '(?m)(^|\s)' + [regex]::Escape($cmd) + '(\s|$)'
    if ($helpResult -notmatch $commandPattern) {
        $missingCommands += $cmd
    }
}
if ($missingCommands.Count -gt 0) {
    throw "Windows launcher missing required commands: $($missingCommands -join ', ')"
}

$doctorHelp = & $ExePath doctor --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher doctor --help failed with exit code ${LASTEXITCODE}: $doctorHelp"
}
if ($doctorHelp -notmatch '--report') {
    throw "Windows launcher doctor command missing --report flag"
}

$supportHelp = & $ExePath support --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher support --help failed with exit code ${LASTEXITCODE}: $supportHelp"
}

Write-Host "Windows native runtime contract validated for version $Version."
Write-Host "All required commands present: start, stop, status, doctor, support, version, env, logs"
Write-Host "doctor --report flag verified"
Write-Host "support command verified"
