Write-Host "Checking uv lockfile freshness..." -ForegroundColor Yellow
$result = & "uv" lock --project backend --check 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAILED: uv lockfile is stale or has dependency conflicts" -ForegroundColor Red
    Write-Host $result -ForegroundColor Red
    exit 1
}
Write-Host "uv lockfile is fresh with no dependency conflicts" -ForegroundColor Green
exit 0
