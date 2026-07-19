$ErrorActionPreference = "Stop"

$repoRoot = (git rev-parse --show-toplevel).Trim()
$cursor = [System.IO.DirectoryInfo]::new($repoRoot)
$harnessScript = $null

while ($null -ne $cursor) {
    if ($cursor.Name -eq "Repos") {
        $candidate = Join-Path $cursor.FullName "devtools/harness/tools/pre-commit-checks.ps1"
        if (Test-Path -LiteralPath $candidate) {
            $harnessScript = $candidate
        }
        break
    }
    $cursor = $cursor.Parent
}

if ($null -eq $harnessScript) {
    Write-Error "Could not locate Repos/devtools/harness/tools/pre-commit-checks.ps1 from $repoRoot"
    exit 1
}

$output = @(& $harnessScript *>&1)
$harnessExitCode = $LASTEXITCODE

if ($harnessExitCode -eq 0) {
    $output | ForEach-Object { Write-Host $_ }
    exit 0
}

# The shared harness uses a line-oriented Python check that cannot distinguish
# nested calls such as super().__init__() from module-level calls. Revalidate
# module-call-only failures with the repository's AST architecture test. Other
# harness failures retain the original non-zero result.
$failures = @($output | Where-Object { "$_" -match '^\s*FAIL ' })
$moduleCallFailures = @(
    $failures | Where-Object { "$_" -match 'Module-level function call' }
)
if ($failures.Count -gt 0 -and $failures.Count -eq $moduleCallFailures.Count) {
    Write-Host "  Revalidating Python module-level calls with the AST architecture check..."
    & uv --directory (Join-Path $repoRoot "backend") run --no-sync pytest tests/architecture/test_module_side_effects.py -q
    exit $LASTEXITCODE
}

$output | ForEach-Object { Write-Host $_ }
exit $harnessExitCode
