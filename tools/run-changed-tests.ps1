param([switch]$Fix)
$ErrorActionPreference = "Stop"

$changed = git diff --name-only --cached --diff-filter=ACMR | Where-Object { $_ -match '^backend/tests/.*\.py$' }
if (-not $changed) {
    Write-Host "No test files changed — skipping changed-test run" -ForegroundColor Green
    exit 0
}

$testPaths = $changed -join " "
Write-Host "Running tests for changed files: $testPaths" -ForegroundColor Yellow

# Capture native-command stderr without tripping $ErrorActionPreference = "Stop".
# pytest emits benign teardown warnings (PytestUnraisableExceptionWarning) on
# stderr while still exiting 0; with EAP=Stop those lines abort this script.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$result = & "uv" run --project backend --no-sync pytest --tb=short -q --timeout=120 $testPaths.Split(" ") 2>&1
$exitCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap

if ($exitCode -ne 0) {
    Write-Host "FAILED: Changed tests did not pass" -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    exit 1
}

Write-Host "All changed tests pass" -ForegroundColor Green
exit 0
