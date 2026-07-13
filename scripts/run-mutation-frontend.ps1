<#
.SYNOPSIS
    Run frontend mutation testing in a one-shot Linux Docker container.
.DESCRIPTION
    Builds the Stryker Docker image (if not cached) and runs mutation testing.
    Pass a --mutate glob to target specific files (default: src/stores/**/*.ts).
.EXAMPLE
    .\scripts\run-mutation-frontend.ps1
    .\scripts\run-mutation-frontend.ps1 --mutate "src/composables/**/*.ts"
#>

param(
    [string]$Mutate = "src/stores/**/*.ts"
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ImageName = "modulo-stryker"

Write-Host "Building mutation testing image..." -ForegroundColor Cyan
docker build -t $ImageName `
    -f "$RepoRoot/frontend/Dockerfile.mutation" `
    --ignorefile "$RepoRoot/.dockerignore-mutation" `
    "$RepoRoot"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed" -ForegroundColor Red
    exit 1
}

Write-Host "Running Stryker with --mutate $Mutate" -ForegroundColor Cyan
docker run --rm $ImageName npx stryker run --mutate $Mutate

$LASTEXITCODE
