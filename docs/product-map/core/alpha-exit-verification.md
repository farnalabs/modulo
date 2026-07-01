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
- [x] Alpha documentation exists (dev-setup.md, architecture.md, CONTRIBUTING.md)
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

## Known Gaps
- **No automated checks for criteria #1, #3, #4, #5, #6**: These require human sign-off and cannot be fully automated. The verification script provides structured checklists for each.
- **CI workflow is workflow_dispatch only**: Does not automatically run on push/PR. This is by design — alpha exit is a deliberate decision, not a CI gate.
- **BDD test dependency on Postgres/Redis containers**: Verification requires Docker to be available. When running locally without Docker, the BDD test step is skipped.
- **No frontend lint/type-check in verification**: Adding frontend checks requires Node.js/npm to be available. Could be added as an optional check.
