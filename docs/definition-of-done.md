# Definition of Done — farnalabs/modulo

This is the single Definition of Done for work delivered to this repo. It is used by:
1. Any agent or developer writing code here — as a self-review pass BEFORE reporting a task complete.
2. The PR Reviewer (qa-reviewer skill) — as the checklist it applies to every PR.

If a step cannot be verified in the current environment (e.g. no Docker, no node_modules in a worktree, no running backend), say so explicitly in the report/PR instead of assuming it passed.

## 1. Test suite inventory

Static list of the test suites that exist in this repo, their root paths, what they cover, and how to run a subset. This is an inventory for CONSIDERATION — it is NOT a code-to-test mapping. Each implementer decides which suites are relevant to their change.

| Suite | Root path | Covers | Environment needed | Run a subset |
|---|---|---|---|---|
| Unit | `backend/tests/unit/` | no DB, no Docker, StubModelBackend for all LLM calls, fast. Trigger/cron/SAQ units include `tests/unit/test_cron_helpers.py`, `tests/unit/test_cron_helpers_ongoing.py` (FAR-158 ongoing top-up), `tests/unit/test_saq_worker.py` | uv synced in `backend/` | `uv run pytest tests/unit/<area>/ -q --timeout=120` |
| Architecture | `backend/tests/architecture/` | import-linter architecture contracts | uv | `uv run pytest tests/architecture/ -q` |
| Connectors | `backend/tests/connectors/` | connector conformance | uv | `uv run pytest tests/connectors/<name>/ -q` |
| Integration | `backend/tests/integration/` | real Postgres via testcontainers, Alembic migrations applied first, Factory Boy. Migrations include `test_migrations` round-trips (e.g. `0094_ongoing_trigger_type` NOT VALID/VALIDATE + partial CHECKs + indexes) | Docker | `uv run pytest tests/integration/<file> -q -m integration` (usually CI / merge-queue only) |
| BDD/E2E | `backend/tests/bdd/` (`features/` + `steps/`) | pytest-bdd + Playwright | running backend + Playwright | CI / merge-queue |
| Load | `backend/tests/load/` | performance/load | full stack | CI / manual |
| Frontend unit | `frontend/src/__tests__/` (Vitest; the `tests/unit/` path does not exist) | Vitest | node_modules (absent in worktrees) | `pnpm run test:unit` (main tree or CI) |
| Frontend e2e | `frontend/tests/e2e/` | Playwright; @smoke tag gates merges | running stack | CI |

## 2. Test impact consideration (the "guess")

For every change, BEFORE reporting done:
1. Consider ALL suites above, not just unit tests. A backend change can affect integration/BDD; a frontend change affects Vitest and e2e; a model/schema change can affect architecture contracts.
2. For each suite you believe is affected, guess the specific test files that exercise the code you changed. Derive the guess from evidence — grep the test tree for imports of the changed modules, or match the feature-area directory. Do not guess from memory alone.
3. Run the high-confidence subset where the environment allows: unit/architecture/connectors are cheap in a worktree after `uv sync`; frontend only where node_modules exists; integration/BDD only where Docker/a backend exists.
4. If you cannot confidently map a suite to your change, state that explicitly in your report/PR — do NOT guess-and-fix blind, and do NOT skip silently.
5. Never delete or disable a test to make it pass — fix it in place to reflect the new correct behaviour. If a failure is pre-existing on main and unrelated, report it.
6. Record what you ran and what you deferred in the report/PR body so the reviewer can check.

## 3. Self-review checklist (implementers — run before reporting done)

- [ ] Files exist on disk; footprint matches the task scope; no deletions of tests, features, or docs outside the task
- [ ] Lint clean: ruff / mypy / bandit / semgrep where runnable in this environment
- [ ] No secrets committed
- [ ] PRD accurate: docs/prd.md describes what was actually built (update if drifted)
- [ ] Manifest updated if routes/features changed (frontend/src/manifest.yaml)
- [ ] Test impact considered per section 2; high-confidence subset run where possible; deferred items listed
- [ ] Work committed to the branch; commit message matches repo style

## 4. Reviewer usage (qa-reviewer)

The PR reviewer applies the same checklist to every PR diff:
- Did the implementer consider all suites that could plausibly be affected?
- Is the impact guess plausible — do the cited test files actually exercise the changed code?
- Were the high-confidence subsets run, with results reported?
- Flag "change to X plausibly affects test_y but there is no run evidence" as a major finding.
