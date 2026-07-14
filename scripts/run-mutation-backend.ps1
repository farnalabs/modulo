<#
.SYNOPSIS
    Run backend mutation testing in a one-shot Linux Docker container.
.DESCRIPTION
    Builds the mutmut Docker image (if not cached) and runs mutation testing.
    Pass extra args to mutmut (e.g. --paths-to-mutate) as positional arguments.
.EXAMPLE
    .\scripts\run-mutation-backend.ps1
    .\scripts\run-mutation-backend.ps1 --paths-to-mutate "src/modulo/auth/" "src/modulo/db/rls.py"
#>

param(
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$MutmutArgs
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ImageName = "modulo-mutmut"

# Assemble a clean build context (no .dockerignore to block backend/tests/)
$TempDir = Join-Path $env:TEMP "modulo-mutmut-$(Get-Random)"
$BackendDir = Join-Path $TempDir "backend"
New-Item -ItemType Directory -Path $BackendDir -Force | Out-Null
$Cleanup = { Remove-Item -Path $TempDir -Recurse -Force -ErrorAction SilentlyContinue }

try {
    # Mirror the repo structure so Dockerfile paths (COPY backend/...) resolve
    Copy-Item -Path "$RepoRoot/backend/Dockerfile.mutation" -Destination "$TempDir/Dockerfile"
    Copy-Item -Path "$RepoRoot/backend/pyproject.toml" -Destination "$BackendDir/"
    Copy-Item -Path "$RepoRoot/backend/uv.lock" -Destination "$BackendDir/"
    Copy-Item -Path "$RepoRoot/backend/src" -Destination "$BackendDir/src" -Recurse -Exclude @(".pytest_cache", "__pycache__", "*.pyc")
    Copy-Item -Path "$RepoRoot/backend/tests" -Destination "$BackendDir/tests" -Recurse -Exclude @(".pytest_cache", "__pycache__", "*.pyc")

    Write-Host "Building mutation testing image..." -ForegroundColor Cyan
    docker build -t $ImageName "$TempDir"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Build failed" -ForegroundColor Red
        & $Cleanup
        exit 1
    }

    & $Cleanup

    if ($MutmutArgs.Count -gt 0) {
        Write-Host "Running mutmut with: $($MutmutArgs -join ' ')" -ForegroundColor Cyan
        docker run --rm $ImageName uv run mutmut run @MutmutArgs
    } else {
        Write-Host "Running mutmut with all configured paths..." -ForegroundColor Cyan
        docker run --rm $ImageName
    }
} catch {
    & $Cleanup
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

exit $LASTEXITCODE
