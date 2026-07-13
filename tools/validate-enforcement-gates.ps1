#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Validates that all enforcement gates are structurally intact.
  Runs as a pre-commit hook to prevent weakening of test/CI gates.
  Exit code 0 = all gates intact. Non-zero = violation found.
.DESCRIPTION
  Checks:
  1. No continue-on-error: true on CI validation jobs
  2. verify-main.ps1 uses Fail (not Warn) for all fail-eligible checks
  3. gate.ps1 has Playwright @smoke step
  4. AGENTS.md has Non-Negotiable Enforcement Gates section
#>

$exitCode = 0
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Check($Label, $Condition, $FixHint) {
    if (-not (& $Condition)) {
        Write-Host "  [FAIL] $Label" -ForegroundColor Red
        Write-Host "         Fix: $FixHint" -ForegroundColor Yellow
        $script:exitCode = 1
    } else {
        Write-Host "  [PASS] $Label" -ForegroundColor Green
    }
}

# Check 1: No continue-on-error on product-map-validate or manifest-validate
$ciYml = Join-Path $repoRoot ".github/workflows/ci.yml"
Check "No continue-on-error on CI validation jobs" {
    $content = Get-Content -Raw -LiteralPath $ciYml -ErrorAction SilentlyContinue
    if (-not $content) { return $false }
    $lines = $content -split "`n"
    $inProductMap = $false; $inManifest = $false
    $productMapHasError = $false; $manifestHasError = $false
    foreach ($line in $lines) {
        if ($line -match "product-map-validate:") { $inProductMap = $true; $inManifest = $false; continue }
        if ($line -match "manifest-validate:") { $inManifest = $true; $inProductMap = $false; continue }
        if ($line -match "^\s+\w+:" -and -not ($line -match "(product-map-validate|manifest-validate|continue-on-error)")) { $inProductMap = $false; $inManifest = $false }
        if ($inProductMap -and $line -match "continue-on-error:\s*true") { $productMapHasError = $true }
        if ($inManifest -and $line -match "continue-on-error:\s*true") { $manifestHasError = $true }
    }
    return (-not $productMapHasError -and -not $manifestHasError)
} "Remove continue-on-error: true from product-map-validate and manifest-validate jobs in .github/workflows/ci.yml"

# Check 2: verify-main.ps1 must use Fail (not Warn) for vue-tsc, npm-audit, pip-audit
$verifyMain = "..\..\..\devtools\harness\tools\verify-main.ps1"
$verifyMainPath = Join-Path $repoRoot $verifyMain
Check "verify-main.ps1 uses Fail for vue-tsc" {
    if (-not (Test-Path -LiteralPath $verifyMainPath)) { return $false }
    $content = Get-Content -Raw -LiteralPath $verifyMainPath
    return ($content -notmatch 'Warn.*vue-tsc')
} "Change Warn to Fail for vue-tsc check in $verifyMain"

Check "verify-main.ps1 uses Fail for npm audit" {
    if (-not (Test-Path -LiteralPath $verifyMainPath)) { return $false }
    $content = Get-Content -Raw -LiteralPath $verifyMainPath
    return ($content -notmatch 'Warn.*npm audit')
} "Change Warn to Fail for npm audit check in $verifyMain"

Check "verify-main.ps1 uses Fail for pip-audit" {
    if (-not (Test-Path -LiteralPath $verifyMainPath)) { return $false }
    $content = Get-Content -Raw -LiteralPath $verifyMainPath
    return ($content -notmatch 'Warn.*pip-audit')
} "Change Warn to Fail for pip-audit check in $verifyMain"

# Check 3: gate.ps1 has Playwright @smoke
$gatePs1 = "..\..\..\devtools\harness\tools\gate.ps1"
$gatePs1Path = Join-Path $repoRoot $gatePs1
Check "gate.ps1 has Playwright @smoke step" {
    if (-not (Test-Path -LiteralPath $gatePs1Path)) { return $false }
    $content = Get-Content -Raw -LiteralPath $gatePs1Path
    return ($content -match 'playwright' -or $content -match '@smoke')
} "Add Playwright @smoke step to $gatePs1 (Phase 4b, after merge)"

# Check 4: AGENTS.md has enforcement gates section
$agentsMd = Join-Path $repoRoot "AGENTS.md"
Check "AGENTS.md has Non-Negotiable Enforcement Gates section" {
    if (-not (Test-Path -LiteralPath $agentsMd)) { return $false }
    $content = Get-Content -Raw -LiteralPath $agentsMd
    return ($content -match 'Non-Negotiable Enforcement Gates')
} "Add ## Non-Negotiable Enforcement Gates section to AGENTS.md"

if ($exitCode -eq 0) {
    Write-Host "`nAll enforcement gates intact." -ForegroundColor Green
} else {
    Write-Host "`nENFORCEMENT VIOLATION FOUND - see above." -ForegroundColor Red
    Write-Host "These gates are STRUCTURALLY PROTECTED and must not be weakened." -ForegroundColor Red
}
exit $exitCode
