# Modulo

Product-specific agent guidance for the `farnalabs/modulo` repository.

## What this repo is

Self-hosted Agentic Delivery Governance for agentic SDLC pipelines. Backend:
Python 3.12 FastAPI. Frontend: Vue 3 + Vite SPA on the PrimeVue component
library. Runs on Postgres + Redis.
Product requirements and behaviour spec live in `docs/` and the product map
`frontend/src/manifest.yaml` (ADR 008). The PRD is retired.

## Repository structure

```
modulo/
  backend/                   # Python 3.12, uv, FastAPI
    src/modulo/
      api/                   # FastAPI routes, WebSocket, MCP server
      core/                  # pipeline_engine, schema_registry, trigger_engine, hitl_manager, ...
      db/                    # SQLAlchemy models, Alembic migrations, rls.py, models/
      connectors/            # ConnectorType ABC + connectors/{filesystem,github}
      model_backends/        # BaseChatModel ABC + {anthropic,openai,stub}
      auth/                  # JWT, Basic Auth, API key validation
      otel_bridge/           # LangGraph -> OTel callback handler
    tests/unit/              # No DB, StubModelBackend, fast
    tests/integration/       # Testcontainers Postgres, real migrations
    tests/bdd/               # pytest-bdd steps + Gherkin features
  frontend/                  # Vue 3 (Composition API), Vite, Pinia, pnpm
    src/{stores,components,views,composables}
    src/manifest.yaml        # product map (ADR 008)
    tests/e2e/               # Playwright
  docs/                      # architecture.md, core-principles.md, security/ (ADRs moved out — see below)
  scripts/                   # dev helper scripts
  deploy/                    # Fly/Caddy/nginx/supervisor configs; deploy/compose/ + deploy/docker/ hold non-default compose files and Dockerfiles
  configs/                   # grafana dashboards, otel-collector config
  .semgrep/                  # custom lint rules (rls, credentials, jinja2, yaml, asyncdb)
  .github/workflows/         # CI, Deploy, merge-queue (autonomous PR lifecycle)
```

## Where to look first

- **Product requirements / behaviour:** `docs/architecture.md`, `docs/core-principles.md`
- **Architecture decisions:** ADRs (migrated out of this repo 2026-09-02, FAR-434; previously in `docs/adr/`)
- **Product map:** `frontend/src/manifest.yaml`

## Working-directory rules (non-negotiable)

- **All Python tooling** (`uv run pytest` / `mypy` / `ruff`) runs from `backend/` - never the repo root.
- **All Node tooling** (`pnpm run lint` / `test:unit` / `vue-tsc`) runs from `frontend/` - never the repo root.
- Reproduce the exact command and working directory CI uses when diagnosing a failure - do not add or drop flags.

## Lessons Learned

> **Scope: codebase only.** Lessons recorded here are about the product codebase
> itself: backend, frontend, database schema and migrations, tests, and the
> tooling that runs against the code. Lessons about **deployments**, the
> **dogfood / autonomous SDLC pipeline**, or **developer environments** (machine
> setup, OS quirks, worktrees, CI infrastructure) do **not** belong here; those
> live in the workspace-level `AGENTS.md`.
>
> Relocated out of this file to the workspace-level `AGENTS.md` on 2026-09-17
> ([PR #686](https://github.com/farnalabs/modulo/pull/686)):
>
> - Postgres NUL-byte json-to-jsonb migration gotcha (2026-08-26)
> - E2B sandbox `timeout_seconds` 1-hour cap (2026-08-31)
> - GitHub Actions API 404 means token scope, not 'not found' (2026-09-01)
> - Windows semgrep fail-open wrapper (`run_semgrep.py`) (2026-09-16)
> - Bot PR closures must leave a loud, auditable reason (2026-08-31)

### Repo-wide pnpm overrides silently clobber transitive exact pins (2026-09-06)

A frontend-workspace-wide override (`js-yaml: ^5.0.0` in `frontend/pnpm-workspace.yaml`) broke `pnpm run generate:api` (FAR-646): `@redocly/openapi-core@1.34.19` pins `js-yaml@4.3.1` exactly and reads the `types` export (`js_yaml_1.types.merge`) that js-yaml 5.x removed, so the override hoisted 5.4.1 into it and openapi-typescript crashed at require time — the fix is a scoped override (`@redocly/openapi-core>js-yaml: 4.3.1`, delete it if openapi-typescript ever upgrades to @redocly/openapi-core 2.x, which wants js-yaml ^5.2.2), and note pnpm v10+ reads overrides ONLY from pnpm-workspace.yaml (an `overrides` edit in frontend/package.json is silently ignored, so "fix it in package.json" cannot work).

### Pre-auth routes need an explicit unauthenticated test (2026-09-16)

Every SSO pre-auth route (`/api/v1/auth/oidc/{provider}/login`, `/api/v1/auth/oidc/{provider}/callback`, `/api/v1/auth/saml/login`, `/api/v1/auth/saml/acs`, `/api/v1/auth/saml/metadata`) was guarded with `require_feature("sso")`. `require_feature` resolves the plan via `get_plan_context`, which depends on `get_current_user`, which depends on `HTTPBearer(auto_error=True)`. A request with no `Authorization` header — exactly what a browser sends when navigating from the logged-out login page — raised `401 "Not authenticated"` before the route handler ran. The feature was marked covered in the product map and had passing unit and BDD tests, because every test called those routes with an authenticated client. The real flow (an unauthenticated user clicking the sign-in button) was never exercised, so the feature was unreachable end-to-end while CI stayed green.

Rules:

1. Any route reachable before authentication needs at least one test that calls it with NO credentials and asserts the intended pre-auth behaviour (200/302/400/402 as applicable), and explicitly asserts it is NOT a 401.
2. A dependency that composes an authenticated dependency (`require_feature`, `get_plan_context`, `get_current_user`) must never guard a pre-auth route; use an anonymous resolver instead.
3. "Covered" is a claim about user-reachable behaviour, not about the existence of tests. A feature whose primary entry point is unreachable is not covered.
4. A mocked or authenticated test client does not exercise the unauthenticated path — add the unauthenticated case explicitly.

### Reconstructing a historical value? Copy the consumer's WHOLE precedence chain, not just its first source (2026-10-02)

`POST /runs/{run_id}/nodes/{node_id}/prompt/reveal` built the revealed prompt from the live `Agent` row's `prompt_template`, so editing an agent after a run changed what the endpoint showed for that run. The first fix moved the read to the run's frozen snapshot node. An independent multi-lens quality pass then found — independently, across four lenses — that **dispatch layers a second source on top of that node**: the run's frozen variant override (`_run_overrides["prompt_templates"][agent_id]`, seeded only from `run.variant_config_snapshot`). Reading one source still produced an attestation that was wrong for every A/B `prompt_version` run — the same defect class, one source deeper.

Rules:

1. When an endpoint reconstructs a value that some consumer computes, copy the consumer's **entire decision chain** — every layer, in the same order, with the same guards and the same emptiness checks. A reconstruction that reads only the primary source is correct for the common case and silently wrong for every layered case.
2. Read each layer from the **frozen record the consumer read** — never from a live row, a checkpoint, or caller-influenced input. Where an override namespace exists specifically so caller input can never populate it (here: the system-reserved `_run_overrides`), sourcing it from `run_context`/`input_payload` reintroduces the injection vector that namespace exists to close.
3. Audit the fix for **leftover live reads**. A live-state dependency that survives a "read the frozen record" change — here, a deleted agent still 404ing a historical read whose frozen record held everything needed — is the same defect in stronger form.
4. Prove the reconstruction with a test that pins the layered case, not just the base case. A test that only exercises "snapshot template differs from live template" passes while the variant-override path is still wrong.

### Org-level ledger aggregations must filter `team_id IS NULL` (2026-10-10)

`org_daily_run_counts` is keyed `(organisation_id, team_id, run_date)`; the `team_id IS NULL` row is the ORG-LEVEL total, and `cost_controller.check_and_record_spend` writes it for EVERY terminal run *in addition to* a team row when the run has a team — so the org row already includes team-owned runs and the team rows are an extra breakdown. Any aggregation that sums `run_count` / `total_spend_usd` grouped by date WITHOUT `team_id IS NULL` therefore double-counts every team-owned run.

Two independent sites shipped with the omission: `core/reports/quality_report.py` (`generate_quality_report`'s daily-trend query) and `api/routes/dashboard.py` (`_load_daily_trend`, `_load_trend_run_and_spend`) — each trend disagreed with its own summary, which correctly filtered `team_id IS NULL`.

Rules:

1. Any `select(func.sum(OrgDailyRunCount.run_count/.total_spend_usd))` intended as an ORG-LEVEL figure must include the canonical predicate `org_level_predicate()` imported from `modulo.db.crud.daily_run_count` (which is the `team_id IS NULL` scope) — never a hand-written `OrgDailyRunCount.team_id.is_(None)` literal and never an omitted scope. Only the deliberate per-team breakdown aggregates (`cost_controller` `group_by=team`) omit it.
2. When you add an org-level ledger read, grep for every sibling `OrgDailyRunCount` aggregation in the same change and confirm each applies the same scope — the org-row-includes-team-row model is the thing that makes an unscoped SUM silently wrong.
3. An aggregation that disagrees with the endpoint/report's own summary number is the symptom; a mocked session cannot expose it (the defect is in the SQL SUM) — prove such a fix against a real DB (in-memory SQLite is sufficient; the SUM/filter logic is dialect-independent) with both an org row and a team row seeded for one date.

### A soft-delete-semantics test whose process never imports the ORM session silently tests the wrong thing (2026-10-14)

The global soft-delete filter is registered by an IMPORT SIDE EFFECT: `modulo.db.soft_delete.register_soft_delete_filter()` is what installs the `do_orm_execute` listener that injects `WHERE deleted_at IS NULL` into ORM SELECTs (and `session.get`'s fresh-fetch path), and it runs when `modulo.db.session` is imported. A test module that asserts soft-delete semantics but never imports `modulo.db.session` therefore runs in a process where the filter was never registered — the ORM returns soft-deleted rows and the test asserts the OPPOSITE of production behaviour.

Concretely, during the `collection_install` soft-delete change (the install row is now retired by a `deleted_at` stamp instead of being deleted), `tests/integration/test_library_collection_lifecycle.py` failed in two ways that looked like production bugs: a re-install after uninstall raised "already installed" (the soft-deleted row was still visible to the install guard), and a plain read of the retired row returned the object instead of `None`. Both cleared the moment the module called `register_soft_delete_filter()` at import — the same house pattern already used by sibling integration modules (`test_soft_delete_filter.py`, `test_viewmodel_soft_deleted_team.py`, `test_run_daily_facts.py`, `test_cost_attribution.py`).

Production is unaffected — app startup imports `modulo.db.session`, so the listener is always installed there. This bites test processes only, and it bites in the direction of a FALSE PASS whenever the assertion happens to line up with the unfiltered result, which makes it worse than a flaky test: the suite is green while asserting the inverse of the shipped contract.

Rules:

1. Any test module that asserts soft-delete behaviour (a row is hidden from a plain read / a reinstall succeeds / a count excludes deleted rows) must ensure the filter is registered in its own process — call the idempotent `register_soft_delete_filter()` at module import, as the sibling integration modules do.
2. Do not rely on "some other import probably pulled it in". The registration is a side effect of one specific module (`modulo.db.session`), and whether it has fired depends on which imports happened to run first in that process; assert the precondition explicitly in the module that depends on it (register at import, and — like `test_soft_delete_filter.py` — optionally assert `sqlalchemy.event.contains(SASession, "do_orm_execute", _apply_soft_delete_filter)` so a future removal of the registration fails loudly).
3. When a soft-delete assertion fails in a way that looks like the filter is inverted (a retired row reads as live, a uniqueness guard reports "already installed" after an uninstall), check filter registration in the test process BEFORE debugging the production code.
4. This is a test-process concern only. Production and app startup register the filter normally, so a repro that only fails under pytest and never in the app is the signature of this class.

### A route that returns a failure response instead of raising silently inverts its own audit record (2026-10-15)

The `audited()` / `audited_system()` FastAPI dependencies (`backend/src/modulo/core/audit_coverage.py`) derive the audit `outcome` purely from whether the route handler raised: a normal return stamps `outcome="success"`, a propagating exception stamps `outcome="error"`. So a route that converts a failure into a returned response — a redirect, a soft error body — while guarded by such a dependency records the operation as a SUCCESS. Observed on FAR-1791: the SAML ACS routes were changed to `return` a 303 login redirect on a failed sign-in, and `saml_acs_attempted` (declared `fail_closed=True`) then recorded `outcome="success"` for a DENIED authentication — falsifying the exact security record it exists to provide, and it was silent because no test referenced the event. It also flips the append strategy: the error path is fail-open, the success path fail-closed.

Rules:

1. An audited route signals failure by RAISING, never by returning a failure-shaped response. Convert the exception to the desired HTTP response at the app boundary (a registered exception handler) so the dependency teardown sees the exception and records `outcome="error"` before the handler builds the response.
2. Subclass the project's existing HTTP exception type (so passthrough decorators like `handle_db_errors` re-raise it unchanged) rather than a bare `Exception`.
3. Whenever a change alters what an `audited*`-guarded route does on failure, add a test that asserts the emitted event's `outcome` for BOTH a failed and a successful call — asserting only the HTTP response shape does not pin the audit outcome (the old code would pass those).

### A browser-facing error-contract change isn't done until the BDD/integration tests AND the generated OpenAPI schema are migrated in the same change (2026-10-15)

FAR-1791 changed SSO callback failures from `401/400 + JSON {detail}` to `303 + Location`. The committed unit tests were updated, but the BDD feature files, the integration tests and the committed `frontend/src/lib/api/schema.ts` still asserted/published the OLD contract: the BDD steps called `resp.json()` on the response (which raises on a 303 — CI-breaking), and because a route docstring is emitted verbatim into the OpenAPI spec, the published `schema.ts` still described `401` for a security control — i.e. the public API contract stated the opposite of the shipped behaviour. A follow-up commit then deliberately reverted the accurate docstring to avoid regenerating `schema.ts`, shipping a knowingly false contract rather than regenerating it.

Rules:

1. A status-code / response-body contract change is not complete until every BDD feature/steps and integration test that asserts the old contract is migrated in place — never deleted, disabled or gutted — and they are included in the same change.
2. Any change to a route docstring/description must be followed by regenerating the committed `frontend/src/lib/api/schema.ts` in the SAME change, using the pinned generator (not the worktree-broken `pnpm run generate:api`), and the resulting diff must be limited to the intended text (report it if it is not).
3. Never "fix" a schema-freshness hook by reverting accurate documentation to match a stale generated file — regenerate the file.
