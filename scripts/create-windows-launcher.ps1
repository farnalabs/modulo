#!/usr/bin/env pwsh
# Create Windows launcher placeholder
# Usage: create-windows-launcher.ps1 -Version <version> -OutputPath <path>

param(
    [Parameter(Mandatory=$true)]
    [string]$Version,

    [Parameter(Mandatory=$true)]
    [string]$OutputPath
)

$launcherContent = @"
#!/bin/sh
# This is a placeholder - real Windows build would produce a native .exe
# For now, we create a stub that shows the structure
echo "Modulo Windows installer - placeholder executable"
echo "Version: $Version"
echo "Architecture: amd64"
"@

Set-Content -Path $OutputPath -Value $launcherContent -Encoding UTF8
Write-Host "Created launcher at $OutputPath for version $Version"
