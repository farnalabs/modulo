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

- [x] Backend directory not found → machine check FAIL (script line 113-115)
- [x] Pytest execution failure (exception) → machine check FAIL (script line 145-148)
- [x] Pytest BDD non-zero exit → machine check FAIL (script line 136-144)
- [x] Git log failure → logged, machine check continues (script line 167-168)
- [x] ruff check failure → machine check FAIL (script line 191-197)
- [x] Unit test failure → machine check FAIL (script line 216-226)
- [x] Report file write failure → warning only, no machine check failure (script line 437-439)
- [x] Missing documentation file → logged as fixable issue, no machine check failure (CheckFileExists function)
- [x] Missing FilesystemConnector directory → machine check FAIL (script line 268)
- [x] Missing GitHubConnector directory → machine check FAIL (script line 269)
- [x] CI workflow cleanup containers on failure → continues via always() + Continue error action
- [x] CI workflow step failure → continues to verification script via continue-on-error: true

## Known Gaps
- **Criteria #1, #3, #4, #5, #6 require manual sign-off**: These require human walkthroughs and cannot be fully automated by design.
- **CI workflow is workflow_dispatch only**: Does not automatically run on push/PR. This is by design — alpha exit is a deliberate decision, not a CI gate.
- **BDD test dependency on Postgres/Redis containers**: Verification requires Docker to be available. When running locally without Docker, BDD tests are skipped.
- **No frontend lint/type-check in verification**: Frontend has `lint` and `type-check` npm scripts available. Could be added as an optional script step, but requires Node.js/npm in the CI runner.
- **No Docker availability check in verify-alpha-exit.ps1**: The verification script assumes Docker is available when running BDD tests. If Docker is not running, the pytest call will fail with an opaque error.
- **Temp file collision in RunPytest/RunTool**: Uses [System.IO.Path]::GetTempFileName() which creates a zero-byte file. On systems with aggressive temp file cleanup, this could race with pytest output.
