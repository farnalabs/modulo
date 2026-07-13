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
# Resolve the caller's checkout so proposed worktree changes are validated.
Try { $gitRoot = git rev-parse --show-toplevel 2>$null } Catch { }
if ($gitRoot) {
  $ModuloRoot = (Resolve-Path -LiteralPath $gitRoot).Path
} else {
  # Fallback for non-git contexts (manual testing only)
  $ModuloRoot = Split-Path -Parent $PSScriptRoot
}
# Devtools is a sibling of the primary checkout, while worktrees are nested
# beneath that checkout. Resolve the common Git directory for that sibling.
Try { $commonDir = git rev-parse --git-common-dir 2>$null } Catch { }
if ($commonDir) {
  $primaryModuloRoot = Split-Path -Parent (Resolve-Path -LiteralPath $commonDir).Path
  $DevtoolsRoot = Join-Path (Split-Path -Parent $primaryModuloRoot) "devtools"
} else {
  $DevtoolsRoot = Join-Path (Split-Path -Parent $ModuloRoot) "devtools"
}

function Check($Label, $Condition, $FixHint) {
    if (-not (& $Condition)) {
        Write-Host "  [FAIL] $Label" -ForegroundColor Red
        Write-Host "         Fix: $FixHint" -ForegroundColor Yellow
        $script:exitCode = 1
    } else {
        Write-Host "  [PASS] $Label" -ForegroundColor Green
    }
}

# Check 1: Validation failures must fail their CI job directly.
$ciYml = Join-Path $ModuloRoot ".github/workflows/ci.yml"
Check "No continue-on-error on CI validation jobs" {
    $content = Get-Content -Raw -LiteralPath $ciYml -ErrorAction SilentlyContinue
    if (-not $content) { return $false }
    return ($content -notmatch '(?m)^\s*continue-on-error:\s*true\s*$')
} "Remove continue-on-error: true from validation steps in .github/workflows/ci.yml"

# Check 2: verify-main.ps1 must use Fail (not Warn) for vue-tsc, npm-audit, pip-audit
$verifyMainPath = Join-Path $DevtoolsRoot "harness/tools/verify-main.ps1"
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
$gatePs1Path = Join-Path $DevtoolsRoot "harness/tools/gate.ps1"
Check "gate.ps1 has Playwright @smoke step" {
    if (-not (Test-Path -LiteralPath $gatePs1Path)) { return $false }
    $content = Get-Content -Raw -LiteralPath $gatePs1Path
    return ($content -match 'playwright' -or $content -match '@smoke')
} "Add Playwright @smoke step to $gatePs1 (Phase 4b, after merge)"

# Check 4: AGENTS.md has enforcement gates section
$agentsMd = Join-Path $ModuloRoot "AGENTS.md"
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
