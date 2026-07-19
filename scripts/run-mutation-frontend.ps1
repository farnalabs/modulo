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

# Assemble a clean build context (no .dockerignore to block frontend/tests/)
$TempDir = Join-Path $env:TEMP "modulo-stryker-$(Get-Random)"
$FrontendDir = Join-Path $TempDir "frontend"
New-Item -ItemType Directory -Path $FrontendDir -Force | Out-Null
$Cleanup = { Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue }

try {
    Copy-Item -Path "$RepoRoot/frontend/Dockerfile.mutation" -Destination "$TempDir/Dockerfile"
    Copy-Item -Path "$RepoRoot/frontend/package.json" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/package-lock.json" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/stryker.config.json" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/src" -Destination "$FrontendDir/src" -Recurse
    Copy-Item -Path "$RepoRoot/frontend/vite.config.ts" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/tsconfig.json" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/tsconfig.app.json" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/index.html" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/env.d.ts" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/postcss.config.js" -Destination "$FrontendDir/"
    Copy-Item -Path "$RepoRoot/frontend/tailwind.config.cjs" -Destination "$FrontendDir/"

    Write-Host "Building Stryker image..." -ForegroundColor Cyan
    docker build -t $ImageName "$TempDir"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Build failed" -ForegroundColor Red
        & $Cleanup
        exit 1
    }

    & $Cleanup

    Write-Host "Running Stryker with --mutate $Mutate" -ForegroundColor Cyan
    docker run --rm $ImageName npx stryker run --mutate $Mutate
} catch {
    & $Cleanup
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

exit $LASTEXITCODE
