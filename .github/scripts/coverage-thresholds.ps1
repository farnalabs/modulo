<#
.SYNOPSIS
    Runs unit tests with per-module coverage threshold enforcement.
.DESCRIPTION
    Runs pytest with overall coverage measurement, then checks per-module
    coverage thresholds using the existing .coverage data file.
    Exit code is non-zero if any threshold is breached.
.PARAMETER Parallelism
    Number of parallel workers for pytest (-n argument). Default: auto.
#>

param(
    [string]$Parallelism = "auto"
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Name)
    Write-Host "::group::$Name"
}

function End-Step {
    Write-Host "::endgroup::"
}

function Check-LastExit {
    param([string]$Module, [int]$Threshold)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "::error::$Module coverage below $Threshold% threshold"
        exit 1
    }
}

# Step 1: Run unit tests with overall coverage
Write-Step "Running unit tests (overall coverage threshold: 80%)"
uv run --no-sync pytest tests/unit/ -n $Parallelism --cov=src/modulo --cov-report=xml --cov-report=term-missing --cov-fail-under=80 -q
Check-LastExit -Module "Overall" -Threshold 80
End-Step

# Step 2: Per-module coverage checks
Write-Step "Per-module coverage thresholds"

Write-Host "Checking modulo.auth (threshold: 90%)"
uv run --no-sync coverage report --include="src/modulo/auth/*" --fail-under=90
Check-LastExit -Module "modulo.auth" -Threshold 90

Write-Host "Checking modulo.core.pipeline_engine (threshold: 85%)"
uv run --no-sync coverage report --include="src/modulo/core/pipeline_engine/*" --fail-under=85
Check-LastExit -Module "modulo.core.pipeline_engine" -Threshold 85

Write-Host "Checking modulo.db.rls (threshold: 95%)"
uv run --no-sync coverage report --include="src/modulo/db/rls.py" --fail-under=95
Check-LastExit -Module "modulo.db.rls" -Threshold 95

End-Step

Write-Host "All coverage thresholds met."
