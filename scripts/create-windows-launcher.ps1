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

Write-Host "Windows native runtime prerequisite validated for version $Version."
Write-Host "No placeholder launcher written; a real PE executable is required before publication."
