param([switch]$Fix)
# NOTE: do NOT set `$ErrorActionPreference = "Stop"` here. With Stop active,
# `$result = & native ... 2>&1` converts the native command's STDERR writes
# (e.g. pytest PytestUnraisableExceptionWarning) into a TERMINATING error on
# the first stderr line, aborting the script before `$LASTEXITCODE` is read —
# the hook then fails even though the tests passed. Rely on `$LASTEXITCODE`
# after the invocation instead (the `-match` filter below is the only place
# that needs a hard stop, and it is handled inline).

$changed = git diff --name-only --cached --diff-filter=ACMR | Where-Object { $_ -match '^backend/tests/unit/.*\.py$' }
if (-not $changed) {
    Write-Host "No unit test files changed - skipping changed-test run (integration tests are covered by CI with Docker)" -ForegroundColor Green
    exit 0
}

# Run pytest from backend/ (not the repo root) so backend/.env resolves for
# Settings(). The hook runs from the repo root, where .env is not found and
# Settings() cannot construct (DATABASE_URL / SECRET_KEY / FERNET_KEY required).
# Strip the repo-root 'backend/' prefix so the paths resolve from backend/.
$testPaths = @($changed | ForEach-Object { $_ -replace '^backend/', '' })
Write-Host "Running tests for changed files: $($testPaths -join ' ')" -ForegroundColor Yellow

$backendDir = Join-Path $PSScriptRoot "..\backend"
Push-Location $backendDir
try {
    $result = & "uv" run --no-sync pytest --tb=short -q --timeout=120 $testPaths 2>&1
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Host "FAILED: Changed tests did not pass" -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    exit 1
}

Write-Host "All changed tests pass" -ForegroundColor Green
exit 0
