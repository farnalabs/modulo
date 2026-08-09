# Test Suite Audit — Disabled / Weakened Tests (2026-08-08)

Audit of `.github/workflows/*.yml`, `backend/tests/`, and `frontend/tests/e2e/` for
tests or suites that are disabled, skipped, xfailed, or otherwise weakened.

**Scope:** read-only inventory. No code, workflow, or test was modified. This file is
the only artefact produced so a human can decide what to re-enable / fix / track.

---

## 1. `continue-on-error: true` in CI workflows

Three occurrences exist. Two are legitimate (aggregate present); **one is broken —
the aggregate references a step id that does not exist, so the gate never fires.**

### 1.1 `deploy.yml:335` — staging E2E — OK (aggregate present)

- File: `.github/workflows/deploy.yml`
- Line 335: `continue-on-error: true` on **"Run @regression Playwright tests against staging"** (`id: regression-tests`, line 327).
- Aggregate: `deploy.yml:344-349` — `if: steps.regression-tests.outcome == 'failure'` → `exit 1`.
  - Step id matches. The aggregate correctly re-raises on failure. **Not weakened.**
- Verdict: **VALID** (allowed pattern per AGENTS.md gate rule 1). No action.

### 1.2 `deploy.yml:506` — prod smoke — **BROKEN aggregate (gate silently passes)**

- File: `.github/workflows/deploy.yml`
- Line 506: `continue-on-error: true` on **"Run @smoke Playwright tests against app.modulo.run"**.
  This step has **NO `id:`** — nothing names it `smoke-tests`.
- Aggregate: `deploy.yml:508-513` — `if: steps.smoke-tests.outcome == 'failure'` → `exit 1`.
  **`steps.smoke-tests.outcome` is a dangling reference.** GitHub Actions evaluates it to
  empty/undefined (never `'failure'`), so the "Fail on smoke test failure" step is **never
  reached**. Production smoke-test failures are silently swallowed and the job reports green.
- Verdict: **CRITICAL — weakens a production gate.** Fix: add `id: smoke-tests` to the
  step at `deploy.yml:498`. (This contradicts the pre-audit assumption that the aggregate existed — it exists syntactically but references a non-existent step id.)
- Recommendation: **Fix immediately** (one-line `id:` addition, separate PR). T-shirt: XS.

### 1.3 `merge-queue.yml:48` — main CI check — no aggregate (already known, being fixed separately)

- File: `.github/workflows/merge-queue.yml`
- Line 48: `continue-on-error: true` on **"Check main CI status"** (`id: check-ci`).
- No `if: failure()` aggregate. The step explicitly logs `"Main CI is $CI_CONCLUSION - proceeding anyway."` when main is not green (`main_ci_ok=false`) and the job continues.
- Verdict: **WEAKENED** — per the audit brief this is already being fixed separately, so it is recorded here for completeness only. No new action.

---

## 2. pytest skips / xfails / conditional skips — `backend/tests/`

### 2.1 FEATURE_NOT_IMPLEMENTED — skips hiding product gaps (highest-value category)

| file:line | marker | reason | impact | recommendation |
|---|---|---|---|---|
| `backend/tests/bdd/steps/test_cost_controls.py:43,48,55,62,69` | `pytest.skip(...)` | "Per-agent token budget enforcement is not yet implemented" | Skips the BDD scenario **"Token budget enforced"** in `features/costs/cost_controls.feature:11` (steps 12-16). PRD feature: per-agent token budget. | Implement per-agent token budget OR raise Linear ticket; then remove the skips. |
| `backend/tests/bdd/steps/test_cost_controls.py:284,291,296,301,306,311,318,325,330,335` | `pytest.skip(...)` | "Circuit breaker is not yet implemented" | Skips BDD scenarios **"Circuit breaker trips…"** and **"Circuit breaker resets…"** in `features/costs/cost_controls.feature:47,55`. PRD feature: per-pipeline cost circuit breaker. | Implement circuit breaker OR raise Linear ticket; then remove the skips. |
| `backend/tests/bdd/steps/test_alpha_users.py:278` | `pytest.skip(...)` | "team-scope run-trigger enforcement deferred per ADR 017 (Phase 3)" | Backs the `@skip`-tagged scenario **"Runner role is scoped to pipelines they own"** in `features/users/runner_role.feature:15`. Feature deferred to Phase 3. | Track in Linear (ADR 017 Phase 3); leave skipped until implemented, or drop the scenario. |
| `backend/tests/bdd/features/users/runner_role.feature:15` | `@skip` tag | team-scope run-trigger enforcement deferred | The scenario is skipped at the pytest-bdd level; the step also self-skips. | Same as above — track as Phase 3 work. |
| `backend/tests/unit/api/test_cost_controls_bdd.py:107` | `pytest.skip(...)` (asserted via `pytest.raises`) | "Per-agent token budget enforcement is not yet implemented" | Meta-test `test_token_budget_step_raises_skip` **asserts the stub still skips** — no behaviour is covered, only that the skip exists. | Replace with real step coverage when the feature lands. |
| `backend/tests/unit/api/test_cost_controls_bdd.py:277` | `pytest.skip(...)` (asserted) | "Circuit breaker is not yet implemented" | Meta-test `test_circuit_breaker_step_raises_skip` — same as above, no behaviour covered. | Replace with real step coverage when the feature lands. |

**Total: 5 BDD scenarios + 2 meta-tests hiding two unbuilt PRD features (per-agent token budget, circuit breaker) + 1 deferred ADR 017 scenario.**

### 2.2 FLAKY — skipped due to flakiness

| file:line | marker | reason | recommendation |
|---|---|---|---|
| `backend/tests/integration/crud/test_connector_instance.py:79` | `@pytest.mark.skip` | "flaky: asyncpg connection race in teardown" | Fix the flake (asyncpg teardown race) then re-enable. The skipped test `test_update_connector_instance_unknown_returns_none` covers a real behaviour (update of unknown id returns None). T-shirt: S. |

### 2.3 ENV_CONDITIONAL — skipped when env not available

These are reasonable but each should be verified to be narrow:

| file:line | marker | condition | verdict |
|---|---|---|---|
| `backend/tests/integration/test_schemathesis.py:42` | `@pytest.mark.skipif` (module) | `_redis_reachable()` — REDIS_URL unset/unreachable. CI (deploy.yml) starts Redis so it runs there. | **Reasonable.** Note: it is a **module-level** skipif evaluated at import — Redis must be up before the module imports; on any runner where Redis is slow to start, the whole fuzz suite silently skips. Verify CI actually runs it. |
| `backend/tests/bdd/steps/test_personas.py:1808` | `pytest.skip(...)` | Docker not installed | Reasonable for a Docker-dependent persona scenario. |
| `backend/tests/bdd/steps/test_personas.py:1817` | `pytest.skip(...)` | Docker daemon not running | Reasonable. |
| `backend/tests/unit/scripts/test_backup.py:291` | `@pytest.mark.skipif` | `openssl not installed` | Reasonable (backup/restore needs openssl CLI). |
| `backend/tests/unit/scripts/test_restore.py:136` | `@pytest.mark.skipif` | `openssl not installed` | Reasonable. |
| `backend/tests/unit/test_devtools_skills.py:26` | `pytest.skip(...)` | Devtools repo not found (sibling/devtools, `DEVTOOLS_PATH`, or `/home/user/devtools`) | **Watch:** this suite only runs when the devtools repo is a sibling. In the farnalabs dev layout it is; in a clean CI checkout of `modulo` alone it silently skips. Confirm CI runs it (it validates every skill's frontmatter). |
| `backend/tests/unit/test_devtools_skills.py:36` | `pytest.skip(...)` | Skills dir missing under devtools path | Same as above. |

### 2.4 ACTIVE marker exclusion — `awaiting-implementation` (in use, by design) + one DEAD_CONFIG item (quarantine plugin)

**Correction to the initial draft:** the `awaiting-implementation` marker is **NOT** dead
config. `@awaiting-implementation` is applied to **55 scenarios across 11 feature files**
— `personas/{alice-devx-sme,elena-engineering-director,jordan-community-contributor,marcus-ciso,priya-platform-engineer}`,
`pipelines/{scheduling,webhook_trigger,run_variants}`, `plugins/plugin_registry`,
`variants/variant_groups`, `workflows/import`. All of those feature files are collected
via `scenarios(...)` in the corresponding step files (`test_personas.py:16-21`,
`test_pipelines.py:19-27`, `test_plugin_registry.py:18`, `test_variant_groups.py:12`,
`test_workflows.py:13`). pytest-bdd applies scenario tags as pytest markers, so the
`-m 'not awaiting-implementation'` addopts (`pyproject.toml:249`) is **actively deselecting
those 55 scenarios** — the exclusion is doing exactly its job, not silently hiding anything.

| file:line | item | detail | recommendation |
|---|---|---|---|
| `backend/pyproject.toml:249` | `addopts = "-m 'not awaiting-implementation'"` | The `awaiting-implementation` marker is excluded from every run. | **Keep.** The marker is in active use (55 tagged scenarios, all collected via `scenarios(...)`); the exclusion is deselecting them by design. Reversed from the original draft — do NOT remove the marker or the addopts entry. Optionally add a CI assertion that pins the deselected set (so a newly `@awaiting-implementation`-tagged scenario is only deselected deliberately), rather than removing the exclusion. T-shirt: XS. |
| `backend/pyproject.toml:254` | `"awaiting-implementation"` marker definition | Defined and applied — see the 55 tagged scenarios above. | Keep — the marker is in active use. |
| `backend/tests/quarantine_plugin.py` | Flaky-test quarantine plugin | Reads `.quarantine.yml` and xfails listed tests. **The plugin is never registered** (no `pytest_plugins` in any conftest; pyproject `plugins` is mypy's pydantic plugin, not pytest). And `.quarantine.yml` currently lists **zero** quarantined tests. | Either register the plugin (so a future quarantine actually applies) or delete both files. Dead safety net that gives a false sense of protection. T-shirt: XS. |

### 2.5 OTHER — benign / per-case conditional skips (recorded, no action)

| file:line | marker | reason | verdict |
|---|---|---|---|
| `backend/tests/unit/library/test_schema_seeds.py:190` | `pytest.skip(...)` | Schema has no required fields | Legitimate per-parametrize-case guard. No action. |
| `backend/tests/integration/test_audit_immutability.py:188` | `pytest.skip(...)` | Route file is exempt from audit-event requirement | Legitimate allow-list logic. No action. |
| `backend/tests/integration/test_audit_immutability.py:206` | `pytest.skip(...)` | Route file has no mutating routes | Legitimate. No action. |
| `backend/tests/architecture/test_fly_toml_python_version.py:29` | `pytest.skip(...)` | backend/pyproject.toml not found | Structural guard; in-repo file always present. No action. |
| `backend/tests/architecture/test_fly_toml_python_version.py:44` | `pytest.skip(...)` | No python base image found in backend Dockerfiles | Would only fire if all 4 Dockerfiles were missing. No action. |

*(The many `pytestmark = [pytest.mark.integration, ...]`, `asyncio`, `xdist_group`,
`usefixtures`, and `filterwarnings` lines found during the sweep are marks, not skips —
no coverage is lost by them and they are not listed as findings.)*

---

## 3. Playwright / frontend exclusions

- `frontend/playwright.config.ts` — **no** `testIgnore`, `testMatch`, or `grep` filters
  that exclude suites. `--grep "@smoke"` is invoked only from the `test:e2e:smoke` npm
  script (subset selection, by design). Nothing excluded in the config.
- No `test.fixme` or `test.only` anywhere in `frontend/tests/e2e/`.
- 12 conditional `test.skip(env.name !== 'local', ...)` in 6 files — these skip **on
  non-local targets** (staging/app) and run only against the local mock. All are
  ENV_CONDITIONAL by design (local mock API fixtures):

  | file | lines | reason |
  |---|---|---|
  | `admin-create-user.spec.ts` | 5, 29, 48 | Uses `setupLocalMockApi` — only runs locally |
  | `copy-pipeline.spec.ts` | 5, 16 | Requires a pipeline in the database |
  | `first-run-golden-path.spec.ts` | 4 | Requires local mock with specific pipeline data |
  | `navigation.spec.ts` | 5 | Requires a pipeline in the database |
  | `pipelines.spec.ts` | 6, 24 | Requires a pipeline in the database |
  | `view-modes-admin.spec.ts` | 82, 155, 177 | Uses `selectOption` on non-native select — view-type dropdown is not a `<select>` element |

  Verdict: these are legitimate local-only tests. **Note:** `view-modes-admin.spec.ts`
  skips on non-local because the dropdown isn't a native `<select>` — worth confirming the
  local runs actually cover those flows in CI, but the tests themselves are not disabled
  outright. No action required; flagged for awareness.

---

## 4. Summary

### 4.1 Counts per category

| Category | Count | Detail |
|---|---|---|
| **Broken CI gate (continue-on-error w/ dangling aggregate)** | **1** | `deploy.yml:506` prod smoke — gate silently passes |
| **Weakend CI gate (continue-on-error, no aggregate)** | 1 | `merge-queue.yml:48` (already being fixed separately) |
| **FEATURE_NOT_IMPLEMENTED** skips | 7 | 5 BDD scenarios (token budget ×1, circuit breaker ×2, ADR 017 team-scope ×1 scenario + 1 extra from unit meta-tests) — spanning 6 skip calls in `test_cost_controls.py`, 1 in `test_alpha_users.py`, 2 meta-tests in `test_cost_controls_bdd.py` |
| **FLAKY** skips | 1 | `test_connector_instance.py:79` |
| **ENV_CONDITIONAL** skips | 9 | schemathesis (module-level), personas ×2, backup/restore openssl ×2, devtools-skills ×2 (broad), fly-toml ×2 (structural) |
| **DEAD_CONFIG** | 1 | unregistered quarantine plugin (the `awaiting-implementation` marker + addopts exclusion are in active use — see §2.4) |
| **OTHER** (benign) | 3 | schema-seeds, audit-immutability ×2 |
| **Playwright / frontend** | 12 | all `env.name !== 'local'` conditionals — legitimate |

### 4.2 Ranked actions (1 = highest priority)

1. **Fix the broken prod-smoke gate — `deploy.yml:506`/`508-513`.** The
   `if: steps.smoke-tests.outcome` aggregate references a step id that doesn't exist, so
   production smoke-test failures are silently ignored. Add `id: smoke-tests` to the step
   at line 498. (One line; XS.) Highest priority because it is a production gate that
   **silently passes**.
2. **Track / implement the feature-not-implemented gaps** — per-agent token budget
   (cost_controls.feature:11) and circuit breaker (cost_controls.feature:47,55). These are
   PRD features whose BDD scenarios are permanently skipped; raise Linear tickets and remove
   the skips when implemented. Also track ADR 017 Phase 3 team-scope run-trigger enforcement
   (runner_role.feature:15).
3. **Fix the flaky skip** — `test_connector_instance.py:79` (asyncpg teardown race); fix
   the flake and re-enable the lost behaviour test.
4. **Verify env-conditionals are narrow** — specifically confirm CI actually runs the
   schemathesis fuzz (module-level skipif can silently skip if Redis is slow) and the
   devtools-skills suite (silently skips when devtools repo isn't a sibling).
5. **Remove dead config** — the only genuine dead item is the unregistered quarantine
   plugin (`backend/tests/quarantine_plugin.py` + `.quarantine.yml`): register it or delete
   both. Do **not** touch the `awaiting-implementation` marker / addopts exclusion — it is
   in active use (55 tagged scenarios across 11 feature files, deselected by design); if a
   guard is wanted, add a CI assertion that pins the deselected set instead. XS.

---

*Generated by an audit Worker on 2026-08-08. Read-only — no tests, workflows, or config were changed.*
