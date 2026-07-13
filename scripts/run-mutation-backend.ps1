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

Write-Host "🔨 Building mutation testing image..." -ForegroundColor Cyan
docker build -t $ImageName -f "$RepoRoot/backend/Dockerfile.mutation" "$RepoRoot"
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Build failed" -ForegroundColor Red
    exit 1
}

if ($MutmutArgs.Count -gt 0) {
    $ArgStr = $MutmutArgs -join " "
    Write-Host "🧪 Running mutmut with: $ArgStr" -ForegroundColor Cyan
    docker run --rm $ImageName uv run mutmut run @MutmutArgs
} else {
    Write-Host "🧪 Running mutmut (all configured paths)..." -ForegroundColor Cyan
    docker run --rm $ImageName
}

Write-Host "`n📊 Mutation results saved in container artifacts. Re-run with --paths-to-mutate to target specific files." -ForegroundColor Cyan
