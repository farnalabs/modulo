<#
.SYNOPSIS
    Verifies all 6 alpha exit criteria from PRD §10.3b.
.DESCRIPTION
    Runs machine-verifiable checks (BDD test pass/fail, git log presence)
    and prints a human-verifiable checklist for criteria requiring manual sign-off.
.EXIT CODE
    0 = all machine checks pass
    1 = machine check failed
    2 = script error
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# --- Paths ---
$scriptRoot = $PSScriptRoot
$productRoot = Resolve-Path (Join-Path $scriptRoot "..")
$backendDir = Join-Path $productRoot "backend"
$reportPath = Join-Path $productRoot "alpha-exit-report.txt"

$reportLines = [System.Collections.Generic.List[string]]::new()
$machinePassed = $true

# --- Helpers ---
function Log($msg) {
    Write-Host $msg
    $script:reportLines.Add($msg)
}

function LogHeader($msg) {
    Log ""
    Log $msg
    Log ("=" * $msg.Length)
}

function LogCheckbox($checked, $label) {
    $box = if ($checked) { "[X]" } else { "[ ]" }
    Log "$box $label"
}

function RunPytest($dir, $args_) {
    $original = Get-Location
    try {
        Set-Location -LiteralPath $dir
        $output = uv run pytest $args_ 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
        return @{ Output = $output; ExitCode = $exitCode }
    } finally {
        Set-Location -LiteralPath $original
    }
}

# ==============================================================
#  HEADER
# ==============================================================
$dateStr = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Log ""
Log "╔══════════════════════════════════════════════════════════════╗"
Log "║          Alpha Exit Verification Report                      ║"
Log "║          Generated: $dateStr"
Log "╚══════════════════════════════════════════════════════════════╝"
Log ""

# ==============================================================
#  MACHINE-VERIFIABLE CRITERIA
# ==============================================================
LogHeader "Machine-Verifiable Criteria"
Log ""

# --- Criterion #2: All happy-path BDD scenarios green in CI ---
LogHeader "Criterion #2: All happy-path BDD scenarios green in CI"
Log ""

if (-not (Test-Path -LiteralPath $backendDir)) {
    Log "  ERROR: backend directory not found at $backendDir"
    $machinePassed = $false
} else {
    Log "  Running: pytest tests/bdd/ -x --tb=short -q"
    Log ""

    try {
        $result = RunPytest $backendDir @("tests/bdd/", "-x", "--tb=short", "-q")
        $output = $result.Output
        $exitCode = $result.ExitCode

        Log $output

        if ($exitCode -eq 0) {
            # Parse summary line like "45 passed in 12.34s"
            if ($output -match '(\d+) passed') {
                $passed = $Matches[1]
                $total = $passed
                LogCheckbox $true "BDD scenarios: $passed/$total passing"
            } else {
                LogCheckbox $true "BDD scenarios: all passing"
            }
        } else {
            $machinePassed = $false
            if ($output -match '(\d+) failed') {
                $failed = $Matches[1]
                LogCheckbox $false "BDD scenarios: $failed failing — see output above"
            } else {
                LogCheckbox $false "BDD scenarios: FAILED (exit code $exitCode)"
            }
        }
    } catch {
        $machinePassed = $false
        LogCheckbox $false "BDD scenarios: ERROR — $_"
    }
}

Log ""

# --- Criterion #2 (supplementary): git log check ---
LogHeader "Supplementary: Git log check"
Log ""

try {
    Push-Location $productRoot
    $gitLog = git log --oneline -5 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0 -and $gitLog.Trim().Length -gt 0) {
        Log "  Recent commits:"
        $gitLog.Trim() -split "`n" | ForEach-Object { Log "    $_" }
        LogCheckbox $true "Git repository has commits"
    } else {
        LogCheckbox $false "No commits found in git log"
    }
} catch {
    LogCheckbox $false "Git log check failed: $_"
} finally {
    Pop-Location
}

Log ""

# --- Machine checks summary ---
$machineStatus = if ($machinePassed) { "PASS" } else { "FAIL" }
Log "  Machine checks result: $machineStatus"
Log ""

# ==============================================================
#  HUMAN-VERIFIABLE CRITERIA
# ==============================================================
LogHeader "Human-Verifiable Criteria (requires manual sign-off)"
Log ""
Log "  Each criterion below requires a human to verify and sign off."
Log ""

# --- Criterion #1: Demo pipeline walkable by 3 non-authors ---
LogHeader "Criterion #1: Demo pipeline walkable by 3 non-authors"
Log ""
Log "  [ ] Criterion #1: Demo pipeline walkable"
Log "      ┌─ How to verify"
Log "      ├─ 1. Start Modulo with MODULO_DEMO_MODE=true"
Log "      ├─ 2. Load the demo pipeline (prd-to-requirements)"
Log "      ├─ 3. Walk through the full pipeline end-to-end"
Log "      ├─ 4. Repeat with 2 additional people who did NOT author the code"
Log "      └─ Each walker should complete without assistance"
Log ""
Log "      Verification log:"
Log "      Walker #1: ___ (name) ___ (date) ___ (signed)"
Log "      Walker #2: ___ (name) ___ (date) ___ (signed)"
Log "      Walker #3: ___ (name) ___ (date) ___ (signed)"
Log ""

# --- Criterion #3: Non-demo pipeline ---
LogHeader "Criterion #3: At least one non-demo pipeline built and run to completion"
Log ""
Log "  [ ] Criterion #3: Non-demo pipeline"
Log "      ┌─ How to verify"
Log "      ├─ 1. An internal user (not the demo author) builds a pipeline"
Log "      ├─ 2. The pipeline uses real connectors (not demo stubs)"
Log "      ├─ 3. The pipeline runs to completion without errors"
Log "      └─ 4. The output artifacts are inspectable and correct"
Log ""
Log "      Pipeline name: ___"
Log "      Built by: ___"
Log "      Run ID: ___"
Log "      Completed at: ___ (date) ___ (signed)"
Log ""

# --- Criterion #4: HITL approve/reject by 2 different users ---
LogHeader "Criterion #4: HITL approve/reject by 2 different users"
Log ""
Log "  [ ] Criterion #4: HITL approve/reject by 2 different users"
Log "      ┌─ How to verify"
Log "      ├─ 1. Configure MODULO_USERS with at least 2 entries"
Log "      ├─ 2. User A creates a pipeline with a HITL gate"
Log "      ├─ 3. Run the pipeline until it reaches the HITL gate"
Log "      ├─ 4. User B claims the HITL request"
Log "      ├─ 5. User B approves the request — pipeline continues"
Log "      ├─ 6. In a second run, User B rejects — pipeline stops"
Log "      └─ 7. Both outcomes are visible in run inspection"
Log ""
Log "      MODULO_USERS configured: ___"
Log "      Reviewer (User B): ___"
Log "      Approve run ID: ___"
Log "      Reject run ID: ___"
Log "      Verified by: ___ (date) ___ (signed)"
Log ""

# --- Criterion #5: Connector swap ---
LogHeader "Criterion #5: Connector swap (Filesystem ↔ GitHub)"
Log ""
Log "  [ ] Criterion #5: Connector swap demonstrated"
Log "      ┌─ How to verify"
Log "      ├─ 1. Create a pipeline bound to FilesystemConnector"
Log "      ├─ 2. Run the pipeline to completion — verify output"
Log "      ├─ 3. Rebind the pipeline to GitHubConnector (same schema)"
Log "      ├─ 4. Run the pipeline again — verify equivalent output"
Log "      └─ 5. Both runs produce correct, inspectable results"
Log ""
Log "      Filesystem run ID: ___"
Log "      GitHub run ID: ___"
Log "      Verified by: ___ (date) ___ (signed)"
Log ""

# --- Criterion #6: Run Context ---
LogHeader "Criterion #6: Run Context demonstrated"
Log ""
Log "  [ ] Criterion #6: Run Context demonstrated"
Log "      ┌─ How to verify"
Log "      ├─ 1. Create a pipeline with a context-setter agent (e.g. complexity-reviewer)"
Log "      ├─ 2. The context-setter must be the first node in the pipeline"
Log "      ├─ 3. Run the pipeline"
Log "      ├─ 4. In run inspection, verify the context-setter's output"
Log "      ├─ 5. Verify a downstream agent's behaviour visibly changed"
Log "      └─    based on the context-setter's output"
Log ""
Log "      Pipeline name: ___"
Log "      Context-setter agent: ___"
Log "      Downstream agent that changed: ___"
Log "      Run ID: ___"
Log "      Verified by: ___ (date) ___ (signed)"
Log ""

# ==============================================================
#  FINAL SUMMARY TABLE
# ==============================================================
LogHeader "Summary"
Log ""
Log " ┌──────┬────────────────────────────────────────────────────┬──────────┐"
Log " │ Crit │ Description                                        │ Status   │"
Log " ├──────┼────────────────────────────────────────────────────┼──────────┤"
$c1Status = "HUMAN"
$c2Status = if ($machinePassed) { "PASS" } else { "FAIL" }
$c3Status = "HUMAN"
$c4Status = "HUMAN"
$c5Status = "HUMAN"
$c6Status = "HUMAN"
Log " │  1   │ Demo pipeline walkable by 3 non-authors            │ $c1Status  │"
Log " │  2   │ All happy-path BDD scenarios green in CI           │ $c2Status  │"
Log " │  3   │ Non-demo pipeline built and run to completion       │ $c3Status  │"
Log " │  4   │ HITL approve/reject by 2 different users           │ $c4Status  │"
Log " │  5   │ Connector swap (Filesystem ↔ GitHub)               │ $c5Status  │"
Log " │  6   │ Run Context demonstrated                           │ $c6Status  │"
Log " └──────┴────────────────────────────────────────────────────┴──────────┘"
Log ""
Log "  Machine checks: $machineStatus"
Log "  Human checks: PENDING (requires 6 manual sign-offs above)"
Log ""

# --- Write report to file ---
try {
    $finalReport = $reportLines -join "`r`n"
    Set-Content -Encoding UTF8 -LiteralPath $reportPath -Value $finalReport
    Log "Report written to: $reportPath"
} catch {
    Log "WARNING: Could not write report file: $_"
}

# --- Exit code ---
if ($machinePassed) {
    exit 0
} else {
    exit 1
}
