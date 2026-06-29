# Cross-platform dev setup helper for Windows
param([switch]$Full)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "Setting up dev environment..."

# Backend
Set-Location "$root/backend"
if (-not (Test-Path -Path ".venv")) {
    uv venv
}
uv sync

# Frontend
Set-Location "$root/frontend"
npm install

# Docker
if ($Full) {
    docker compose -f "$root/docker-compose.yml" up -d
}

Write-Host "Done!"
