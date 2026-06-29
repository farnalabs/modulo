[CmdletBinding()]
param(
    [string]$Version = "latest",
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\Modulo"
)

$ErrorActionPreference = "Stop"
$repo = "farnalabs/modulo"
Write-Host "Installing Modulo CLI v$Version..."

$arch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "386" }
$url = "https://github.com/$repo/releases/$Version/download/modulo-windows-$arch.zip"

Write-Host "Downloading from $url..."
Invoke-WebRequest -Uri $url -OutFile "$env:TEMP\modulo.zip"
Expand-Archive -Path "$env:TEMP\modulo.zip" -DestinationPath $InstallDir -Force
Remove-Item "$env:TEMP\modulo.zip"

Write-Host "Installed to $InstallDir\modulo.exe"
Write-Host "Add $InstallDir to your PATH"
