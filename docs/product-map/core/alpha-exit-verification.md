---
id: feat-core-alpha-exit-verification
prd: 10.3b
delivery-tasks: [task-prd-alpha-exit-verification, task-prd-alpha-exit-verification-mechanism]
bdd: []
code:
  - scripts/verify-alpha-exit.ps1
unit-tests: []
depends-on: [feat-core-pipeline-execution, feat-core-run-context]
status: partial
---

# Alpha Exit Verification

## Behaviours

### Criterion #1: Demo pipeline walkable by 3 non-authors

- [ ] Three non-author walkers have completed the demo pipeline without assistance
- [ ] Each walker's name, date, and sign-off is documented

### Criterion #2: All happy-path BDD scenarios green in CI

- [x] Verification script runs `pytest tests/bdd/ -x --tb=short -q`
- [ ] CI workflow starts Postgres and Redis containers for BDD tests
- [ ] CI workflow runs migrations before tests
- [ ] BDD test failures are reported as workflow annotations
- [ ] Verification report is uploaded as a CI artifact
- [ ] Summary step prints gate status
- [x] Verification script accepts `-SkipBDD` parameter to avoid duplicate test execution in CI
- [ ] CI workflow passes `skip_bdd` input to verification script
- [x] Docker-unavailable detection → BDD tests skipped gracefully with clear message, not opaque failure

### Criterion #3: Non-demo pipeline built and run to completion

- [ ] At least one non-demo pipeline exists and has been run to completion
- [ ] Builder name, pipeline name, and run ID are documented

### Criterion #4: HITL approve/reject by 2 different users

- [ ] Two different named users have demonstrated HITL claim and review
- [ ] MODULO_USERS is configured with at least 2 entries
- [ ] Both approve and reject outcomes are verified in run inspection

### Criterion #5: Connector swap (Filesystem ↔ GitHub)

- [ ] Pipeline runs successfully against FilesystemConnector
- [ ] Same pipeline rebinds to GitHubConnector and runs successfully
- [ ] Both run IDs and verifier sign-off are documented

### Criterion #6: Run Context demonstrated

- [ ] Pipeline uses a context-setter agent (e.g. complexity-reviewer)
- [ ] Context-setter output visibly changes downstream agent behaviour
- [ ] Change is verified in run inspection

### Supplementary machine checks

- [x] ruff check passes
- [x] Backend unit tests pass
- [x] Alpha documentation exists (dev-setup.md was missing — now created; architecture.md, CONTRIBUTING.md exist)
- [x] FilesystemConnector implementation exists
- [x] GitHubConnector implementation exists
- [x] Seed data script exists
- [x] BDD feature files exist

### CI workflow

- [ ] Workflow runs on workflow_dispatch
- [ ] Postgres and Redis are started as Docker containers
- [ ] Backend dependencies are installed via uv
- [ ] Alembic migrations run before tests
- [ ] Verification script runs and reports exit code
- [ ] Report artifact is uploaded with 90-day retention
- [ ] Summary step displays gate result

### Error Handling

- [x] Backend directory not found → machine check FAIL
- [x] Pytest execution failure (exception) → machine check FAIL
- [x] Pytest BDD non-zero exit → machine check FAIL
- [x] Pytest "no tests ran" → detected and reported as FAIL
- [x] Git log failure → logged, machine check continues
- [x] ruff check failure → machine check FAIL
- [x] Unit test failure → machine check FAIL
- [x] Test duplicate execution avoided via -SkipBDD flag (when CI already ran tests)
- [x] Report file write failure → warning only, no machine check failure
- [x] Missing documentation file → logged as fixable issue, no machine check failure (CheckFileExists function)
- [x] Missing FilesystemConnector directory → machine check FAIL
- [x] Missing GitHubConnector directory → machine check FAIL
- [x] Docker unavailable → BDD tests skipped gracefully, clear message, no opaque pytest failure
- [ ] CI workflow cleanup containers on failure → continues via always() + Continue error action
- [ ] CI workflow step failure → continues to verification script via continue-on-error: true
- [ ] BDD scenario skipped vs failed distinction: SkipBDD logs "assumed passing" but has no way to verify CI step outcome

## QA History

- 2026-08-15 (distribute partial-model-backends, round 3): Audit pass. The unchecked items here are (a) human sign-off items (three non-author walkers, named users demonstrating HITL claim/review, builder-name/pipeline/run documentation, verifier sign-offs) that cannot be machine-checked from this repo — they require real human demonstrations and documented evidence; and (b) CI-workflow items (container startup, migrations-before-tests, BDD annotations, report artifact, `skip_bdd` input, cleanup-on-failure, failure-continuation) that live in the verification CI workflow, not in this delivery's allowlist. None are affected by the model-backend deletion-protection change. No tests deleted or disabled. Status: partial.

### Index 163 (2026-07-04)
- **MAJOR fix**: Added `-SkipBDD` switch parameter to verify-alpha-exit.ps1 so the CI workflow can avoid duplicate test execution
- **MAJOR fix**: CI workflow now passes `skip_bdd` input to verification script, preventing re-running BDD tests that CI already executed (lines 134-140)
- **MAJOR fix**: Docker-unavailable detection now skips BDD tests gracefully with clear instructions instead of running pytest to an opaque connection error (lines 126-133)
- **MINOR fix**: Added "no tests ran" edge case detection to pytest output parsing (line 148-151)
- Added 3 new behaviour checkboxes to Criterion #2: SkipBDD parameter, CI passthrough, Docker-unavailable handling
- Added 2 error handling checkboxes: "no tests ran" detection and Docker-unavailable + SkipBDD handling
- Added 2 unchecked error handling items: CI outcome verification gap, hardcoded CI secrets
- Updated Known Gaps: resolved "No Docker availability check" gap (now handled), added 4 new gaps (no retry/backoff, port conflict risk, hardcoded CI secrets, SkipBDD verification gap)

## Known Gaps
- **Criteria #1, #3, #4, #5, #6 require manual sign-off**: These require human walkthroughs and cannot be fully automated by design.
- **CI workflow `alpha-exit-report.yml` was removed**: Deleted in the dead-workflow cleanup (commit b7ecbdf4). Alpha exit is now run locally via `scripts/verify-alpha-exit.ps1` (optionally with `-SkipBDD`). All CI-workflow behaviours in this entry are marked `[ ]` until a replacement CI job is added.
- **BDD test dependency on Postgres/Redis containers**: Verification requires Docker to be available. When running locally without Docker, BDD tests are skipped.
- **No frontend lint/type-check in verification**: Frontend has `lint` and `type-check` npm scripts available. Could be added as an optional script step, but requires Node.js/npm in the CI runner.
- **Temp file collision in RunPytest/RunTool**: Uses [System.IO.Path]::GetTempFileName() which creates a zero-byte file. On systems with aggressive temp file cleanup, this could race with pytest output.
- **No retry/backoff on Docker container readiness**: Wait loops poll every 2s but have no exponential backoff or jitter. Timer-based polling is adequate for the expected single-run pattern.
- **Port conflict risk**: If port 5432 or 6379 is already in use, Docker containers will fail to start. No port-fallback or pre-check logic.
- **SkipBDD cannot distinguish "skipped because CI passed" from "skipped because CI didn't run"**: The verification script logs "assumed passing" but has no mechanism to verify the CI step outcome. A future improvement could parse CI step annotations or accept an explicit pass/fail status parameter.

### 2026-07-12 — Round 3 improve-architecture

Reviewed for B904, exc_info=True, stale frontmatter, dead code. No Python code in this entry (PowerShell/YAML only — `verify-alpha-exit.ps1`). No code changes needed. Frontmatter intact, QA History already complete through Round 2.

### 2026-07-31 — improve-architecture (product-map walk)

- **STALE**: Removed `code:` ref to `.github/workflows/alpha-exit-report.yml` — the workflow was deleted in the dead-workflow cleanup (commit b7ecbdf4). It no longer exists on disk, so the `code:` ref was broken.
- **STALE**: Marked all CI-workflow behaviour checkboxes (Criterion #2, "CI workflow", CI-related error-handling items) `[x]` → `[ ]` — they describe the removed workflow.
- **STALE**: Updated Known Gaps — removed "CI workflow is workflow_dispatch only", "Duplicate test execution in CI", and "CI secrets hardcoded in workflow YAML" gaps (all referenced the deleted workflow); added the workflow-removal gap.

### Index 332 (2026-07-08)
- **BUG fix**: Added `$dateStr = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'` initialization before the report header renders the timestamp (was undefined variable → empty string in report header)
- **STALE**: Marked "Alpha documentation exists" checkbox as `[x]` — docs/dev-setup.md now confirmed on disk
- **VERIFIED**: docs/architecture.md and CONTRIBUTING.md exist on disk — checklist accurate
- **VERIFIED**: Website docs at Website/modulo-website/src/docs/alpha-exit.md already cover PRD §10.3b — no stub needed
