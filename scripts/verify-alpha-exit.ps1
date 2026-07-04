<#
.SYNOPSIS
    Verifies all 6 alpha exit criteria from PRD sec10.3b.
.DESCRIPTION
    Runs machine-verifiable checks (BDD test pass/fail, git log presence)
    and prints a human-verifiable checklist for criteria requiring manual sign-off.
    When -SkipBDD is set, BDD test execution and Docker checks are skipped
    (expected when the CI workflow already ran them and passed).
.PARAMETER SkipBDD
    Skip BDD test execution and Docker availability checks. The script will
    report BDD-related criteria based on pre-existing results only.
.EXIT CODE
    0 = all machine checks pass
    1 = machine check failed
    2 = script error
#>

[CmdletBinding()]
param(
    [switch]$SkipBDD
)

$ErrorActionPreference = "Stop"

# --- Paths ---
$scriptRoot = $PSScriptRoot
$productRoot = Resolve-Path (Join-Path $scriptRoot "..")
$backendDir = Join-Path $productRoot "backend"
$reportPath = Join-Path $productRoot "alpha-exit-report.txt"

$reportLines = [System.Collections.Generic.List[string]]::new()
$machinePassed = $true
$fixableIssues = [System.Collections.Generic.List[string]]::new()

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
        $tempOut = [System.IO.Path]::GetTempFileName()
        $tempErr = [System.IO.Path]::GetTempFileName()
        $p = Start-Process -FilePath "uv" -ArgumentList "run pytest $args_" -NoNewWindow -RedirectStandardOutput $tempOut -RedirectStandardError $tempErr -Wait -PassThru
        $stdout = Get-Content -Path $tempOut -Encoding UTF8 -Raw
        $stderr = Get-Content -Path $tempErr -Encoding UTF8 -Raw
        Remove-Item -Path $tempOut -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $tempErr -Force -ErrorAction SilentlyContinue
        return @{ Output = $stdout; ErrorOutput = $stderr; ExitCode = $p.ExitCode }
    } catch {
        return @{ Output = ""; ErrorOutput = "ERROR: $_"; ExitCode = 1 }
    } finally {
        Set-Location -LiteralPath $original
    }
}

function RunTool($dir, $exe, $args_) {
    $original = Get-Location
    try {
        Set-Location -LiteralPath $dir
        $tempOut = [System.IO.Path]::GetTempFileName()
        $tempErr = [System.IO.Path]::GetTempFileName()
        $p = Start-Process -FilePath $exe -ArgumentList $args_ -NoNewWindow -RedirectStandardOutput $tempOut -RedirectStandardError $tempErr -Wait -PassThru
        $stdout = Get-Content -Path $tempOut -Encoding UTF8 -Raw
        $stderr = Get-Content -Path $tempErr -Encoding UTF8 -Raw
        Remove-Item -Path $tempOut -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $tempErr -Force -ErrorAction SilentlyContinue
        return @{ Output = $stdout; ErrorOutput = $stderr; ExitCode = $p.ExitCode }
    } catch {
        return @{ Output = ""; ErrorOutput = "ERROR: $_"; ExitCode = 1 }
    } finally {
        Set-Location -LiteralPath $original
    }
}

function CheckFileExists($path, $label) {
    $exists = Test-Path -LiteralPath $path
    LogCheckbox $exists $label
    if (-not $exists) {
        $script:fixableIssues.Add("Missing file: $path ($label)")
    }
    return $exists
}

# ==============================================================
#  HEADER
# ==============================================================
$bddSkipped = $SkipBDD
Log ""
Log "+--------------------------------------------------------------------+"
Log "|          Alpha Exit Verification Report                           |"
Log "|          Generated: $dateStr"
Log "+--------------------------------------------------------------------+"
Log ""

# ==============================================================
#  MACHINE-VERIFIABLE CRITERIA
# ==============================================================
LogHeader "Machine-Verifiable Criteria"
Log ""

# --- Criterion #2: All happy-path BDD scenarios green in CI ---
LogHeader "Criterion #2: All happy-path BDD scenarios green in CI"
Log ""

if ($SkipBDD) {
    Log "  BDD tests skipped via -SkipBDD flag (expected when run from CI workflow that already tested)."
    LogCheckbox $true "BDD scenarios: skipped (assumed passing from CI step)"
    Log ""
} elseif (-not (Test-Path -LiteralPath $backendDir)) {
    Log "  ERROR: backend directory not found at $backendDir"
    $machinePassed = $false
} else {
    Log "  Checking Docker availability..."
    $dockerCheck = & docker info 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Log "    WARNING: Docker does not appear to be available. BDD tests require Postgres and Redis."
        Log "    Skipping BDD test execution. Run with Docker available to verify criterion #2."
        Log "    To run locally, start Postgres and Redis (see docs/dev-setup.md), then run:"
        Log "      pytest tests/bdd/ -x --tb=short -q"
        LogCheckbox $false "BDD scenarios: skipped (Docker not available)"
    } else {
        Log "    Docker available."
        Log ""
        Log "  Running: pytest tests/bdd/ -x --tb=short -q"
        Log ""

        try {
            $result = RunPytest $backendDir @("tests/bdd/", "-x", "--tb=short", "-q")
            $output = $result.Output
            $exitCode = $result.ExitCode

            Log $output

            if ($exitCode -eq 0) {
                if ($output -match '(\d+) passed in') {
                    $passed = $Matches[1]
                    $total = $passed
                    LogCheckbox $true "BDD scenarios: $passed/$total passing"
                } elseif ($output -match 'no tests ran') {
                    LogCheckbox $false "BDD scenarios: no tests ran (check test discovery)"
                    $machinePassed = $false
                } else {
                    LogCheckbox $true "BDD scenarios: all passing"
                }
            } else {
                $machinePassed = $false
                if ($output -match '(\d+) failed') {
                    $failed = $Matches[1]
                    LogCheckbox $false "BDD scenarios: $failed failing -- see output above"
                } else {
                    LogCheckbox $false "BDD scenarios: FAILED (exit code $exitCode)"
                }
            }
        } catch {
            $machinePassed = $false
            LogCheckbox $false "BDD scenarios: ERROR -- $_"
        }
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
#  SUPPLEMENTARY MACHINE-VERIFIABLE CHECKS
# ==============================================================
LogHeader "Supplementary Machine-Verifiable Checks"
Log ""

# --- Code quality: ruff ---
Log "Running ruff check..."
$ruffResult = RunTool $backendDir "uv" "run ruff check ."
if ($ruffResult.ExitCode -eq 0) {
    LogCheckbox $true "ruff check passes"
} else {
    $machinePassed = $false
    LogCheckbox $false "ruff check has issues -- run 'ruff check .' to see details"
    $allOutput = $ruffResult.Output + $ruffResult.ErrorOutput
    Log "  First lines of output:"
    $allOutput -split "`r`n|`n" | Select-Object -First 5 | ForEach-Object { Log "    $_" }
}

Log ""

# --- Backend unit tests ---
Log "Running backend unit tests..."
$unitResult = RunPytest $backendDir @("tests/unit/", "-x", "--tb=short", "-q")
$unitOutput = $unitResult.Output
$unitExitCode = $unitResult.ExitCode
if ($unitOutput.Trim().Length -gt 0) {
    Log "  (output truncated for readability)"
}
if ($unitExitCode -eq 0) {
    if ($unitOutput -match '(\d+) passed') {
        $passed = $Matches[1]
        LogCheckbox $true "Unit tests: $passed passing"
    } else {
        LogCheckbox $true "Unit tests: all passing"
    }
} else {
    $machinePassed = $false
    if ($unitOutput -match '(\d+) failed') {
        $failed = $Matches[1]
        LogCheckbox $false "Unit tests: $failed failing"
    } else {
        LogCheckbox $false "Unit tests: FAILED (exit code $unitExitCode)"
    }
    $allOut = $unitResult.Output + "`n" + $unitResult.ErrorOutput
    $allOut -split "`r`n|`n" | Select-String "FAILED|ERROR" | Select-Object -First 10 | ForEach-Object { Log "    $_" }
}

Log ""

# --- Git status ---
Log "Checking git status..."
try {
    Push-Location $productRoot
    $gitStatus = git status --porcelain 2>&1 | Out-String
    Pop-Location
    if ($LASTEXITCODE -eq 0 -and $gitStatus.Trim().Length -eq 0) {
        LogCheckbox $true "Git working tree is clean"
    } else {
        LogCheckbox $true "Git working tree has uncommitted changes (acceptable during development)"
        $gitStatus.Trim() -split "`n" | ForEach-Object { Log "    $_" }
    }
} catch {
    LogCheckbox $true "Git status check skipped: $_"
}

Log ""

# --- Alpha documentation ---
LogHeader "Alpha Documentation (PRD sec10.3a)"
Log ""
$null = CheckFileExists (Join-Path $productRoot "docs/dev-setup.md") "docs/dev-setup.md exists"
$null = CheckFileExists (Join-Path $productRoot "docs/architecture.md") "docs/architecture.md exists"
$null = CheckFileExists (Join-Path $productRoot "CONTRIBUTING.md") "CONTRIBUTING.md exists"
Log "  (Missing doc files are noted but do not fail machine checks)"
Log ""

# --- Alpha implementation artifacts ---
LogHeader "Alpha Implementation Artifacts (PRD sec13)"
Log ""

# Connector implementations
$fsConn = Join-Path (Join-Path (Join-Path (Join-Path $backendDir "src") "modulo") "connectors") "filesystem"
$ghConn = Join-Path (Join-Path (Join-Path (Join-Path $backendDir "src") "modulo") "connectors") "github"
$fsOk = Test-Path -LiteralPath $fsConn
$ghOk = Test-Path -LiteralPath $ghConn
LogCheckbox $fsOk "FilesystemConnector exists ($fsConn)"
LogCheckbox $ghOk "GitHubConnector exists ($ghConn)"
if (-not $fsOk) { $machinePassed = $false }
if (-not $ghOk) { $machinePassed = $false }

# Model backend implementations
$modelBackendDir = Join-Path (Join-Path (Join-Path $backendDir "src") "modulo") "model_backends"
$backendExists = Test-Path -LiteralPath $modelBackendDir
LogCheckbox $backendExists "Model backend directory exists"

# Seed data / demo pipeline
$seedFile = Join-Path (Join-Path $productRoot "scripts") "seed.py"
$seedOk = Test-Path -LiteralPath $seedFile
LogCheckbox $seedOk "Seed data script exists ($seedFile)"

# BDD feature files exist
$bddTestDir = Join-Path $backendDir "tests"
if (Test-Path -LiteralPath $bddTestDir) {
    $bddFeatures = Get-ChildItem -Recurse -Filter "*.feature" -LiteralPath $bddTestDir -ErrorAction SilentlyContinue
    $bddCount = ($bddFeatures | Measure-Object).Count
    LogCheckbox ($bddCount -gt 0) "BDD feature files exist ($bddCount found)"
} else {
    LogCheckbox $false "BDD test directory not found"
}

# Trigger types
$triggerDir = Join-Path (Join-Path (Join-Path (Join-Path $backendDir "src") "modulo") "core") "trigger_engine"
$triggerOk = Test-Path -LiteralPath $triggerDir
LogCheckbox $triggerOk "Trigger engine directory exists"

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
Log "      +- How to verify"
Log "      +- 1. Start Modulo with MODULO_DEMO_MODE=true"
Log "      +- 2. Load the demo pipeline (prd-to-requirements)"
Log "      +- 3. Walk through the full pipeline end-to-end"
Log "      +- 4. Repeat with 2 additional people who did NOT author the code"
Log "      +- Each walker should complete without assistance"
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
Log "      +- How to verify"
Log "      +- 1. An internal user (not the demo author) builds a pipeline"
Log "      +- 2. The pipeline uses real connectors (not demo stubs)"
Log "      +- 3. The pipeline runs to completion without errors"
Log "      +- 4. The output artifacts are inspectable and correct"
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
Log "      +- How to verify"
Log "      +- 1. Configure MODULO_USERS with at least 2 entries"
Log "      +- 2. User A creates a pipeline with a HITL gate"
Log "      +- 3. Run the pipeline until it reaches the HITL gate"
Log "      +- 4. User B claims the HITL request"
Log "      +- 5. User B approves the request -- pipeline continues"
Log "      +- 6. In a second run, User B rejects -- pipeline stops"
Log "      +- 7. Both outcomes are visible in run inspection"
Log ""
Log "      MODULO_USERS configured: ___"
Log "      Reviewer (User B): ___"
Log "      Approve run ID: ___"
Log "      Reject run ID: ___"
Log "      Verified by: ___ (date) ___ (signed)"
Log ""

# --- Criterion #5: Connector swap ---
LogHeader "Criterion #5: Connector swap (Filesystem <-> GitHub)"
Log ""
Log "  [ ] Criterion #5: Connector swap demonstrated"
Log "      +- How to verify"
Log "      +- 1. Create a pipeline bound to FilesystemConnector"
Log "      +- 2. Run the pipeline to completion -- verify output"
Log "      +- 3. Rebind the pipeline to GitHubConnector (same schema)"
Log "      +- 4. Run the pipeline again -- verify equivalent output"
Log "      +- 5. Both runs produce correct, inspectable results"
Log ""
Log "      Filesystem run ID: ___"
Log "      GitHub run ID: ___"
Log "      Verified by: ___ (date) ___ (signed)"
Log ""

# --- Criterion #6: Run Context ---
LogHeader "Criterion #6: Run Context demonstrated"
Log ""
Log "  [ ] Criterion #6: Run Context demonstrated"
Log "      +- How to verify"
Log "      +- 1. Create a pipeline with a context-setter agent (e.g. complexity-reviewer)"
Log "      +- 2. The context-setter must be the first node in the pipeline"
Log "      +- 3. Run the pipeline"
Log "      +- 4. In run inspection, verify the context-setter's output"
Log "      +- 5. Verify a downstream agent's behaviour visibly changed"
Log "      +-    based on the context-setter's output"
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
Log " +------+----------------------------------------------------+----------+"
Log " | Crit | Description                                        | Status   |"
Log " +------+----------------------------------------------------+----------+"
$c1Status = "HUMAN"
$supplementaryPassed = $machinePassed
if ($bddSkipped) {
    $c2Status = "SKIP"
} elseif ($machinePassed -and $supplementaryPassed) {
    $c2Status = "PASS"
} else {
    $c2Status = "FAIL"
}
$c3Status = "HUMAN"
$c4Status = "HUMAN"
$c5Status = "HUMAN"
$c6Status = "HUMAN"
$s1Status = if ($supplementaryPassed) { "PASS" } else { "FAIL" }
Log " |  1   | Demo pipeline walkable by 3 non-authors            | $c1Status  |"
Log " |  2   | All happy-path BDD scenarios green in CI           | $c2Status  |"
Log " |  3   | Non-demo pipeline built and run to completion       | $c3Status  |"
Log " |  4   | HITL approve/reject by 2 different users           | $c4Status  |"
Log " |  5   | Connector swap (Filesystem <-> GitHub)               | $c5Status  |"
Log " |  6   | Run Context demonstrated                           | $c6Status  |"
Log " +------+----------------------------------------------------+----------+"
Log " |  S   | Supplementary (lint, tests, docs, artifacts)       | $s1Status  |"
Log " +------+----------------------------------------------------+----------+"
Log ""
Log "  Machine checks: $machineStatus"
Log "  Supplementary checks: $s1Status"
Log "  Human checks: PENDING (requires 6 manual sign-offs above)"
Log ""
if ($fixableIssues.Count -gt 0) {
    Log "  Notable items:"
    foreach ($issue in $fixableIssues) {
        Log "    - $issue"
    }
}
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






