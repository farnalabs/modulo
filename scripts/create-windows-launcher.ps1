#!/usr/bin/env pwsh
# Validate the Windows native-launcher contract.
#
# This helper never manufactures a fake PE: it only inspects a binary that
# already exists. Two modes, so the dry-run CI step (where no native Windows
# runtime exists yet) and the real packaging path can both be honest:
#
#   default         Validate the output path shape, then report whether a native
#                   binary is present. When it is absent the helper prints a loud
#                   SKIP and exits 0 without creating anything, so the dry-run
#                   step can assert "no fake executable was created". Packaging
#                   itself stays fail-closed in .github/workflows/windows-installer.yml.
#
#   -RequireBinary  Fail closed when the binary is absent, then execute it and
#                   verify every command the installer depends on. Use this only
#                   where a real native runtime is guaranteed to exist.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath,

    [Parameter(Mandatory = $false)]
    [switch]$RequireBinary
)

$ErrorActionPreference = 'Stop'

if ([System.IO.Path]::GetExtension($OutputPath) -ne '.exe') {
    throw "Windows launcher output must be a native .exe path: $OutputPath"
}

$requiredCommands = @('start', 'stop', 'status', 'doctor', 'support', 'version', 'env', 'logs')

if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
    if ($RequireBinary) {
        throw "Windows launcher binary not found at $OutputPath"
    }
    Write-Output "SKIP: no native Windows launcher binary at $OutputPath."
    Write-Output "SKIP: runtime contract checks ($($requiredCommands -join ', ')) were not exercised."
    Write-Output "SKIP: this helper never creates a binary; packaging stays fail-closed in the workflow."
    # return, not exit: the caller is usually a CI step in the same pwsh session.
    return
}

Write-Output "Validating Windows launcher contract for version $Version..."

# Test that the binary runs and shows help
$helpResult = & $OutputPath --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher --help failed with exit code ${LASTEXITCODE}: $helpResult"
}

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
$doctorHelp = & $OutputPath doctor --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher doctor --help failed with exit code ${LASTEXITCODE}: $doctorHelp"
}
if ($doctorHelp -notmatch '--report') {
    throw "Windows launcher doctor command missing --report flag"
}

# Validate support command exists
$supportHelp = & $OutputPath support --help 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Windows launcher support --help failed with exit code ${LASTEXITCODE}: $supportHelp"
}

Write-Output "Windows native runtime contract validated for version $Version."
Write-Output "All required commands present: $($requiredCommands -join ', ')"
Write-Output "doctor --report flag verified"
Write-Output "support command verified"
