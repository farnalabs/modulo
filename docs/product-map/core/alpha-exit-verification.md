---
id: feat-core-alpha-exit-verification
prd: §10.3b
delivery-tasks: [task-prd-alpha-exit-verification, task-prd-alpha-exit-verification-mechanism]
bdd: []
code:
  - scripts/verify-alpha-exit.ps1
  - .github/workflows/alpha-exit-report.yml
unit-tests: []
depends-on: []
status: partial
---

# Alpha Exit Verification

## Behaviours

### Criterion #1: Demo pipeline walkable by 3 non-authors

- [ ] Three non-author walkers have completed the demo pipeline without assistance
- [ ] Each walker's name, date, and sign-off is documented

### Criterion #2: All happy-path BDD scenarios green in CI

- [x] Verification script runs `pytest tests/bdd/ -x --tb=short -q`
- [x] CI workflow starts Postgres and Redis containers for BDD tests
- [x] CI workflow runs migrations before tests
- [x] BDD test failures are reported as workflow annotations
- [x] Verification report is uploaded as a CI artifact
- [x] Summary step prints gate status
- [x] Verification script accepts `-SkipBDD` parameter to avoid duplicate test execution in CI
- [x] CI workflow passes `skip_bdd` input to verification script
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
- [ ] Alpha documentation exists (dev-setup.md was missing — now created; architecture.md, CONTRIBUTING.md exist)
- [x] FilesystemConnector implementation exists
- [x] GitHubConnector implementation exists
- [x] Seed data script exists
- [x] BDD feature files exist

### CI workflow

- [x] Workflow runs on workflow_dispatch
- [x] Postgres and Redis are started as Docker containers
- [x] Backend dependencies are installed via uv
- [x] Alembic migrations run before tests
- [x] Verification script runs and reports exit code
- [x] Report artifact is uploaded with 90-day retention
- [x] Summary step displays gate result

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
- [x] CI workflow cleanup containers on failure → continues via always() + Continue error action
- [x] CI workflow step failure → continues to verification script via continue-on-error: true
- [ ] BDD scenario skipped vs failed distinction: SkipBDD logs "assumed passing" but has no way to verify CI step outcome
- [ ] CI environment variables (SECRET_KEY, FERNET_KEY) hardcoded in workflow YAML rather than GitHub Secrets

## QA History

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
- **CI workflow is workflow_dispatch only**: Does not automatically run on push/PR. This is by design — alpha exit is a deliberate decision, not a CI gate.
- **BDD test dependency on Postgres/Redis containers**: Verification requires Docker to be available. When running locally without Docker, BDD tests are skipped.
- **No frontend lint/type-check in verification**: Frontend has `lint` and `type-check` npm scripts available. Could be added as an optional script step, but requires Node.js/npm in the CI runner.
- **Temp file collision in RunPytest/RunTool**: Uses [System.IO.Path]::GetTempFileName() which creates a zero-byte file. On systems with aggressive temp file cleanup, this could race with pytest output.
- **Duplicate test execution in CI (partially fixed)**: Before the -SkipBDD fix, the CI workflow ran BDD tests → then the verification script ran them again. Now fixed via `skip_bdd` passthrough. When `skip_bdd=false`, duplication still occurs — ideal fix would have the verification script trust CI step outcomes.
- **No retry/backoff on Docker container readiness**: Wait loops poll every 2s but have no exponential backoff or jitter. Timer-based polling is adequate for the expected single-run pattern.
- **Port conflict risk**: If port 5432 or 6379 is already in use on the self-hosted runner, Docker containers will fail to start. No port-fallback or pre-check logic.
- **CI secrets hardcoded in workflow YAML**: SECRET_KEY and FERNET_KEY are plaintext in alpha-exit-report.yml. Acceptable for test-only CI where the runner is isolated, but would be a security concern if the repo were public.
- **SkipBDD cannot distinguish "skipped because CI passed" from "skipped because CI didn't run"**: The verification script logs "assumed passing" but has no mechanism to verify the CI step outcome. A future improvement could parse CI step annotations or accept an explicit pass/fail status parameter.
