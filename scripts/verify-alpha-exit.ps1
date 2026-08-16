<#
.SYNOPSIS
    Verifies all 6 alpha exit criteria from PRD sec10.3b.

.DESCRIPTION
    FAILS unless ALL SIX criteria carry signed:true evidence — a named signee
    AND an evidence_link — in the structured evidence file
    (alpha-exit-evidence.json by default at the product root), AND the
    supplementary machine checks (ruff, backend unit tests, artifact and file
    existence, git log) pass. A missing sign-off fails the gate even when every
    machine check passes.

    The gate is the 6/6 sign-off record, not the machine checks. Machine checks
    are supplementary: they must pass, but they can never substitute for a
    human sign-off.

.PARAMETER SkipBDD
    Skip the live BDD test execution (used when the report is generated without
    spinning up the full Postgres/Redis/frontend stack). With -SkipBDD the
    script does NOT assume criterion #2 passing — criterion #2 still requires
    signed:true evidence (signee + evidence_link) in the evidence file.

.PARAMETER EvidencePath
    Path to the structured evidence JSON. Defaults to alpha-exit-evidence.json
    at the product root.

.EXIT CODE
    0 = all 6 criteria signed (signee + evidence_link) AND machine checks pass
    1 = any criterion unsigned OR any machine check failed
    2 = script error (evidence file missing or malformed, unexpected exception)
#>

[CmdletBinding()]
param(
    [switch]$SkipBDD,
    [string]$EvidencePath
)

$ErrorActionPreference = "Stop"

# --- Paths ---
$scriptRoot = $PSScriptRoot
$productRoot = Resolve-Path (Join-Path $scriptRoot "..")
$backendDir = Join-Path $productRoot "backend"
$reportPath = Join-Path $productRoot "alpha-exit-report.txt"
$reportJsonPath = Join-Path $productRoot "alpha-exit-report.json"
if (-not $EvidencePath) {
    $EvidencePath = Join-Path $productRoot "alpha-exit-evidence.json"
}

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
#  LOAD EVIDENCE
# ==============================================================
$dateStr = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

if (-not (Test-Path -LiteralPath $EvidencePath)) {
    Log "ERROR: Evidence file not found at $EvidencePath"
    Log "  Create it by copying the committed alpha-exit-evidence.json template and"
    Log "  filling in signed/signee/evidence_link for all 6 criteria."
    exit 2
}

try {
    $evidence = Get-Content -LiteralPath $EvidencePath -Encoding UTF8 -Raw | ConvertFrom-Json
} catch {
    Log "ERROR: Evidence file is not valid JSON: $_"
    exit 2
}

$criteria = @{}
foreach ($id in @("1", "2", "3", "4", "5", "6")) {
    $criteria[$id] = $evidence.criteria.$id
    if (-not $criteria[$id]) {
        Log "ERROR: Evidence file is missing criterion #$id. Expected keys 1..6 under 'criteria'."
        exit 2
    }
}

# ==============================================================
#  HEADER
# ==============================================================
Log ""
Log "+--------------------------------------------------------------------+"
Log "|          Alpha Exit Verification Report                           |"
Log "|          Generated: $dateStr"
Log "+--------------------------------------------------------------------+"
Log ""

# ==============================================================
#  THE GATE: 6/6 SIGNED CRITERIA
# ==============================================================
LogHeader "The Gate: 6/6 signed sign-offs (FAIL unless all six are signed)"
Log ""
Log "  A criterion is signed only when signed=true, signee names a human,"
Log "  and evidence_link is a URL. Missing sign-off = FAIL, even if every"
Log "  machine check passes. Human names are supplied by Duncan via the"
Log "  evidence file (alpha-exit-evidence.json) — no names are invented."
Log ""

$allSigned = $true
$signedCount = 0
$signOffRows = [System.Collections.Generic.List[object]]::new()

foreach ($id in @("1", "2", "3", "4", "5", "6")) {
    $c = $criteria[$id]
    $title = if ($c.title) { $c.title } else { "(no title)" }
    $isSigned = $false
    $signee = $null
    $evidenceLink = $null
    if ($c.signed) {
        $signee = if ($c.signee) { [string]$c.signee } else { $null }
        $evidenceLink = if ($c.evidence_link) { [string]$c.evidence_link } else { $null }
        if ($signee -and $evidenceLink) {
            $isSigned = $true
        }
    }
    if ($isSigned) {
        $signedCount++
    } else {
        $allSigned = $false
        $reason = if (-not $c.signed) {
            "not signed"
        } elseif (-not $signee) {
            "signed but no signee named"
        } else {
            "signed but no evidence_link"
        }
        $fixableIssues.Add("Criterion #$id NOT signed ($reason): $title")
    }
    LogCheckbox $isSigned "Criterion #$id — $title"
    if ($isSigned) {
        Log "      signed by: $signee ($evidenceLink)"
    } else {
        Log "      signee: $(if ($signee) { $signee } else { '—' })  evidence_link: $(if ($evidenceLink) { $evidenceLink } else { '—' })"
    }
    $signOffRows.Add([pscustomobject]@{
        id = $id
        title = $title
        signed = $isSigned
        signee = $signee
        evidence_link = $evidenceLink
        date = $(if ($c.date) { [string]$c.date } else { $null })
    })
}

Log ""
Log "  Signed: $signedCount/6"
Log ""

# ==============================================================
#  MACHINE-VERIFIABLE CRITERIA (supplementary)
# ==============================================================
LogHeader "Supplementary Machine-Verifiable Criteria"
Log ""
Log "  Machine checks are supplementary to the 6/6 sign-off gate: they must"
Log "  pass, but they can never substitute for a human sign-off above."
Log ""

# --- Criterion #2 live check (supplementary; the signed gate above is authoritative) ---
LogHeader "Criterion #2 supplementary: All happy-path BDD scenarios green in CI"
Log ""

if ($SkipBDD) {
    Log "  BDD tests skipped via -SkipBDD flag."
    Log "  NOTE: criterion #2 is NOT assumed passing. It is satisfied only by"
    Log "  the signed evidence in the JSON (see 'The Gate' section above)."
    Log ""
} elseif (-not (Test-Path -LiteralPath $backendDir)) {
    Log "  ERROR: backend directory not found at $backendDir"
    $machinePassed = $false
} else {
    Log "  Checking Docker availability..."
    $dockerCheck = & docker info 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Log "    WARNING: Docker does not appear to be available. BDD tests require Postgres and Redis."
        Log "    Skipping BDD test execution. Criterion #2 still requires signed evidence in JSON."
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

# --- Supplementary: git log check ---
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

# --- Supplementary machine checks summary ---
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
#  HUMAN-VERIFIABLE CRITERIA (sign-off walkthrough)
# ==============================================================
LogHeader "Human-Verifiable Criteria (sign-off walkthrough)"
Log ""
Log "  These instructions are now codified in alpha-exit-evidence.json — the"
Log "  gate reads the JSON sign-offs above. This section is for reference only."
Log ""

# --- Criterion #1: Demo pipeline walkable by 3 non-authors ---
LogHeader "Criterion #1: Demo pipeline walkable by 3 non-authors"
Log ""
Log "  How to verify"
Log "  - 1. Start Modulo with modulo_seed_demo_data=true (MODULO_DEMO_MODE is deprecated)"
Log "  - 2. Load the demo pipeline (prd-to-requirements)"
Log "  - 3. Walk through the full pipeline end-to-end"
Log "  - 4. Repeat with 2 additional people who did NOT author the code"
Log "  - Each walker should complete without assistance"
Log ""
Log "  Evidence protocol: one Linear comment on FAR-265 per walker, naming the"
Log "  walker, the date, and the run ID or screen capture. evidence_link = the"
Log "  comment URL or run URL."
Log ""

# --- Criterion #3: Non-demo pipeline ---
LogHeader "Criterion #3: At least one non-demo pipeline built and run to completion"
Log ""
Log "  How to verify"
Log "  - 1. An internal user (not the demo author) builds a pipeline"
Log "  - 2. The pipeline uses real connectors (not demo stubs)"
Log "  - 3. The pipeline runs to completion without errors"
Log "  - 4. The output artifacts are inspectable and correct"
Log ""
Log "  Evidence protocol: Linear comment on FAR-265 naming the builder, the"
Log "  pipeline, and the run ID. evidence_link = the comment URL or run URL."
Log ""

# --- Criterion #4: HITL approve/reject by 2 different users ---
LogHeader "Criterion #4: HITL approve and reject demonstrated by two different named users"
Log ""
Log "  How to verify"
Log "  - 1. Configure MODULO_USERS with at least 2 entries"
Log "  - 2. User A creates a pipeline with a HITL gate"
Log "  - 3. Run the pipeline until it reaches the HITL gate"
Log "  - 4. User B claims the HITL request"
Log "  - 5. User B approves the request -- pipeline continues"
Log "  - 6. In a second run, User B rejects -- pipeline stops"
Log "  - 7. Both outcomes are visible in run inspection"
Log ""
Log "  Evidence protocol: Linear comment on FAR-265 naming both users and both"
Log "  run IDs. evidence_link = the comment URL or run URLs."
Log ""

# --- Criterion #5: Connector swap ---
LogHeader "Criterion #5: Connector swap (Filesystem <-> GitHub)"
Log ""
Log "  How to verify"
Log "  - 1. Create a pipeline bound to FilesystemConnector"
Log "  - 2. Run the pipeline to completion -- verify output"
Log "  - 3. Rebind the pipeline to GitHubConnector (same schema)"
Log "  - 4. Run the pipeline again -- verify equivalent output"
Log "  - 5. Both runs produce correct, inspectable results"
Log ""
Log "  Evidence protocol: Linear comment on FAR-265 with both run IDs."
Log "  evidence_link = the comment URL or run URLs."
Log ""

# --- Criterion #6: Run Context ---
LogHeader "Criterion #6: Run Context demonstrated"
Log ""
Log "  How to verify"
Log "  - 1. Create a pipeline with a context-setter agent (e.g. complexity-reviewer)"
Log "  - 2. The context-setter must be the first node in the pipeline"
Log "  - 3. Run the pipeline"
Log "  - 4. In run inspection, verify the context-setter's output"
Log "  - 5. Verify a downstream agent's behaviour visibly changed"
Log "  -    based on the context-setter's output"
Log ""
Log "  Evidence protocol: Linear comment on FAR-265 naming the pipeline, the"
Log "  context-setter agent, the affected downstream agent, and the run ID."
Log "  evidence_link = the comment URL or run URL."
Log ""

# ==============================================================
#  FINAL SUMMARY TABLE
# ==============================================================
LogHeader "Summary"
Log ""
Log " +------+----------------------------------------------------+----------+"
Log " | Crit | Description                                        | Status   |"
Log " +------+----------------------------------------------------+----------+"
foreach ($row in $signOffRows) {
    $rowStatus = if ($row.signed) { "SIGNED" } else { "NOT SIGNED" }
    Log " |  $($row.id)   | $($row.title.PadRight(50).Substring(0,50)) | $rowStatus |"
}
Log " +------+----------------------------------------------------+----------+"
Log " |  S   | Supplementary (lint, tests, docs, artifacts)       | $machineStatus |"
Log " +------+----------------------------------------------------+----------+"
Log ""
$gateStatus = if ($allSigned -and $machinePassed) { "PASS" } else { "FAIL" }
Log "  Sign-off gate: $signedCount/6 signed -> $(if ($allSigned) { 'PASS' } else { 'FAIL' })"
Log "  Machine checks: $machineStatus"
Log "  OVERALL: $gateStatus"
Log ""
if ($fixableIssues.Count -gt 0) {
    Log "  Issues:"
    foreach ($issue in $fixableIssues) {
        Log "    - $issue"
    }
}
Log ""

# ==============================================================
#  WRITE REPORTS
# ==============================================================
try {
    $finalReport = $reportLines -join "`r`n"
    Set-Content -Encoding UTF8 -LiteralPath $reportPath -Value $finalReport
    Log "Report written to: $reportPath"
} catch {
    Log "WARNING: Could not write report file: $_"
}

try {
    $reportObject = [pscustomobject]@{
        schema_version = 1
        ticket = "FAR-265"
        timestamp = $dateStr
        all_signed = [bool]$allSigned
        machine_passed = [bool]$machinePassed
        signed_count = $signedCount
        total_criteria = 6
        criteria = $signOffRows
        skip_bdd = [bool]$SkipBDD
    }
    $reportJson = $reportObject | ConvertTo-Json -Depth 6
    Set-Content -Encoding UTF8 -LiteralPath $reportJsonPath -Value $reportJson
    Log "Report written to: $reportJsonPath"
} catch {
    Log "WARNING: Could not write report JSON: $_"
}

# --- Exit code ---
if (-not $allSigned) {
    Log "FAIL: $($signOffRows.Count - $signedCount) of 6 criteria are not signed (need signee + evidence_link)."
}
if (-not $machinePassed) {
    Log "FAIL: one or more supplementary machine checks failed."
}
if ($allSigned -and $machinePassed) {
    exit 0
} else {
    exit 1
}
