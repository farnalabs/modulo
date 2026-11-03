#!/usr/bin/env pwsh
# Validate the Windows native-launcher contract without manufacturing a fake PE.
# A real Windows runtime must provide modulo.exe before installer publication.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

if ([System.IO.Path]::GetExtension($OutputPath) -ne '.exe') {
    throw "Windows launcher output must be a native .exe path: $OutputPath"
}

# Validate that the Windows launcher binary exists and supports required commands
$exePath = $OutputPath
if (-not (Test-Path $exePath)) {
    throw "Windows launcher binary not found at $exePath"
}

Write-Host "Validating Windows launcher contract for version $Version..."

# Test that the binary runs and shows help
$helpResult = & $exePath --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher --help failed with exit code ${LASTEXITCODE}: $helpResult"
}

# Verify required commands exist in help output
$requiredCommands = @('start', 'stop', 'status', 'doctor', 'support', 'version', 'env', 'logs')
$missingCommands = @()
foreach ($cmd in $requiredCommands) {
    if ($helpResult -notmatch "\b$cmd\b") {
        $missingCommands += $cmd
    }
}
if ($missingCommands.Count -gt 0) {
    throw "Windows launcher missing required commands: $($missingCommands -join ', ')"
}

# Validate doctor --report flag exists
$doctorHelp = & $exePath doctor --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher doctor --help failed with exit code ${LASTEXITCODE}: $doctorHelp"
}
if ($doctorHelp -notmatch '--report') {
    throw "Windows launcher doctor command missing --report flag"
}

# Validate support command exists
$supportHelp = & $exePath support --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher support --help failed with exit code ${LASTEXITCODE}: $supportHelp"
}

Write-Host "Windows native runtime contract validated for version $Version."
Write-Host "All required commands present: start, stop, status, doctor, support, version, env, logs"
Write-Host "doctor --report flag verified"
Write-Host "support command verified"
