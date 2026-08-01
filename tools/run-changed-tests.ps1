param([switch]$Fix)
$ErrorActionPreference = "Stop"

$changed = git diff --name-only --cached --diff-filter=ACMR | Where-Object { $_ -match '^backend/tests/.*\.py$' }
if (-not $changed) {
    Write-Host "No test files changed — skipping changed-test run" -ForegroundColor Green
    exit 0
}

$testPaths = $changed -join " "
Write-Host "Running tests for changed files: $testPaths" -ForegroundColor Yellow

$result = & "uv" run --project backend --no-sync pytest --tb=short -q --timeout=120 $testPaths.Split(" ") 2>&1
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host "FAILED: Changed tests did not pass" -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    exit 1
}

Write-Host "All changed tests pass" -ForegroundColor Green
exit 0
