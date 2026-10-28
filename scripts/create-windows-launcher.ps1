#!/usr/bin/env pwsh
# Create the Windows launcher placeholder.
# Usage: create-windows-launcher.ps1 -Version <version> -OutputPath <path>
#
# The real Windows launcher is a native PE produced by the Windows native build
# (FAR-679). Until that lands, this writes a batch-text placeholder so the
# installer bundle layout (modulo.exe at the bundle root, referenced by
# modulo.iss and the NT SERVICE ImagePath) can be staged and validated
# end-to-end. It is NOT executable as a native binary and must not ship in a
# promoted release.

param(
    [Parameter(Mandatory=$true)]
    [string]$Version,

    [Parameter(Mandatory=$true)]
    [string]$OutputPath
)

$launcherContent = @"
@echo off
REM Modulo Windows launcher - placeholder, not a native binary (FAR-679).
echo Modulo Windows installer - placeholder executable
echo Version: $Version
echo Architecture: amd64
"@

$parent = Split-Path -Parent $OutputPath
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

Set-Content -Path $OutputPath -Value $launcherContent -Encoding UTF8
Write-Host "Created launcher at $OutputPath for version $Version"
