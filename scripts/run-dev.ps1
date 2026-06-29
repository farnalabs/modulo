# Helper for local dev on Windows
param(
    [switch]$Backend,
    [switch]$Frontend,
    [switch]$Db
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if ($Db) {
    Write-Host "Starting DB services..."
    docker compose -f "$root/docker-compose.yml" up -d postgres redis
}

if ($Backend) {
    Write-Host "Starting backend..."
    Set-Location "$root/backend"
    uv run uvicorn modulo.api.main:app --reload --port 8000
}

if ($Frontend) {
    Write-Host "Starting frontend..."
    Set-Location "$root/frontend"
    npm run dev
}

if (-not $Backend -and -not $Frontend -and -not $Db) {
    Write-Host "Usage: .\scripts\run-dev.ps1 [-Backend] [-Frontend] [-Db]"
}
