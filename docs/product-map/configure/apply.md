---
id: feat-apply
prd: N/A
adr: []
code:
  - backend/src/modulo/cli/apply/__init__.py
  - backend/src/modulo/cli/apply/loader.py
  - backend/src/modulo/cli/apply/models.py
  - backend/src/modulo/cli/apply/plan.py
  - backend/src/modulo/cli/apply/executor.py
  - backend/src/modulo/cli/apply/drift.py
  - backend/src/modulo/cli/apply/pipeline_apply.py
  - backend/src/modulo/cli/apply/trigger_apply.py
unit-tests:
  - backend/tests/unit/cli/test_apply_cli.py
  - backend/tests/unit/cli/test_apply_loader.py
  - backend/tests/unit/cli/test_apply_models.py
  - backend/tests/unit/cli/test_apply_plan.py
  - backend/tests/unit/cli/test_apply_executor.py
  - backend/tests/unit/cli/test_apply_pipeline.py
  - backend/tests/unit/cli/test_apply_trigger.py
  - backend/tests/unit/cli/test_apply_drift.py
  - backend/tests/unit/api/test_apply_api_key_auth.py
bdd: []
depends-on:
  - feat-schemas
  - feat-model-backends
  - feat-pipelines
  - feat-triggers
status: covered
---

# Declarative Configuration CLI (`modulo apply`)

`modulo apply -f <config.yaml>` applies an organisation's schemas (+ versions),
model backends, pipelines and triggers to a live deployment from one YAML file
(`api_version: modulo.dev/v1`), driven by `MODULO_URL` + `MODULO_API_KEY` (a
bearer `mk_` org key). The reference routes are the product surfaces the CLI
writes to (`/schemas`, `/admin/model-backends`, `/pipelines`,
`/settings/triggers`); there is no dedicated CLI page in the nav. Built on the
schemas, model-backends, pipelines and triggers features.

## Behaviours

- [x] `modulo apply -f <file>` loads single- or multi-document YAML
      (`api_version` `modulo.dev/v1` major-gated, minor suffixes merge
      leniently) declaring `entities.schemas` / `entities.model_backends` /
      `entities.pipelines` / `entities.triggers`; duplicate names per kind
      (triggers keyed `pipeline/name`) and trigger forward-references to a
      pipeline declared only in a LATER document are load-time errors
      (`backend/src/modulo/cli/apply/loader.py`,
      `backend/tests/unit/cli/test_apply_loader.py`)
- [x] Secrets are refs-only: backend `api_key` and trigger `config_json`
      secret-shaped entries must be `${env:VAR}` or `secretref://<key>`
      references — inline secret literals are forbidden at validation;
      `${env:}` refs resolve client-side (missing/empty var blocks that
      entity), `secretref://` is blocked pending server-side resolution, and
      `hmac_secret` / `signing_secret` are Fernet-encrypted server-side on
      write (`backend/src/modulo/cli/apply/models.py`,
      `test_apply_models.py`)
- [x] Planning is name-based upsert scoped to the org (the HTTP transport is
      the operator's `MODULO_URL` + `MODULO_API_KEY` bearer org key):
      per-entity decisions `created` / `updated` / `unchanged` / `blocked`,
      hashed over canonical managed-field views (`api_key` excluded; trigger
      `config_json` compared on the desired key set with masked secrets
      stripped symmetrically and `daily_spend_limit` quantised to the column's
      4dp scale) — runtime state (`next_fire_at`, `streak_epoch`, ...) never
      produces drift (`backend/src/modulo/cli/apply/plan.py`,
      `test_apply_plan.py`)
- [x] Entities apply in dependency order `schemas -> model_backends ->
      pipelines -> triggers` with per-entity containment (one failure moves
      that entity to failed, the rest still apply); graphs are cached by
      canonical hash (a graph write snapshots the pipeline, so an unchanged
      graph must not churn snapshots); provider mismatches on backends and
      same-version-different-content schema versions are blocked (never
      silently diverged); missing/ambiguous agent or duplicated live-name
      references are blocked, apply never auto-creates agents
      (`backend/src/modulo/cli/apply/executor.py`,
      `test_apply_executor.py`, `test_apply_pipeline.py`,
      `test_apply_trigger.py`)
- [x] Backend writes are verified: the server-side health-check endpoint
      re-runs after each create/update and an unhealthy result moves the entity
      to `failed` (exit 1) — a stored-but-broken credential is never reported
      as plain success (`test_apply_executor.py`)
- [x] `--refresh-secrets` re-sends trigger configs whose secrets rotated,
      because the server masks stored secrets and cannot tell a rotated value
      from an unchanged one (`backend/src/modulo/cli/apply/__init__.py`,
      `test_apply_executor.py`)
- [x] Exit codes: `--dry-run`/`--plan` always exits 0; real apply exits 1 when
      any entity was blocked or failed (including backend health-check
      failures); `--diff` exits 0 when the org matches the config and 1 when
      drift is detected (created/updated/blocked) so CI can gate on config
      drift (`test_apply_cli.py`)
- [x] `--diff` (drift mode) is read-only: it fetches the live org state and
      reports the plan-shaped report explicitly labelled `mode=drift` WITHOUT
      writing anything (the run returns after the plan phase — no
      POST/PATCH/PUT code path is reachable), adds a node-level breakdown for
      drifted pipelines (`drift_detail`: graph node/edge added/removed/modified
      matched by id; top-level-only drift gets no entry), and the table output
      prefixes verbs (`drift create` / `drift update`) plus per-graph drift
      detail lines (`backend/src/modulo/cli/apply/drift.py`,
      `test_apply_drift.py`)
- [x] Output: human-friendly table (`render_table`: create/update/block/fail/
      unchanged lines + summary counts, or drift-prefixed variants) or
      `--output json` / `--json` (the full report); 401 surfaces "check
      `MODULO_API_KEY`", 404 "server does not expose endpoint ... / older
      version", network errors surface as "apply failed: ..."
      (`test_apply_cli.py`)
- [x] The API-key transport is org-scoped and operator-role gated; an
      unauthenticated or wrong-role key is rejected before any write
      (`backend/tests/unit/api/test_apply_api_key_auth.py`)

## Known Gaps

- **No BDD feature file** — the CLI is covered by the unit + golden-snapshot
  suites (`backend/tests/unit/cli/*`, `backend/tests/unit/cli/golden/*.json`);
  there is no pytest-bdd surface for `modulo apply`.
- **`--diff` graph detail is pipeline-only** — drift in schemas / model
  backends / triggers reports at the top level with no per-entity breakdown.

## QA History

- 2026-09-10: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for the registered manifest feature `feat-apply`
  (FAR-681), which had `product_map` refs from four routes but no
  `docs/product-map/` entry, so the graph root's "all registered manifest
  features have a tracker" promise was false. Behaviours verified against
  `backend/src/modulo/cli/apply/*`, the `tests/unit/cli/test_apply_*` suites
  and `tests/unit/api/test_apply_api_key_auth.py`. Status: covered.
