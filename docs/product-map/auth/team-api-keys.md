---
id: feat-auth-team-api-keys
prd: 9.3
delivery-tasks: [task-nv1-team-api-keys]
bdd:
  - backend/tests/bdd/features/auth/api_keys.feature
code:
  - backend/src/modulo/db/models/api_key.py
  - backend/src/modulo/auth/api_key.py
  - backend/src/modulo/api/routes/api_keys.py
  - backend/src/modulo/db/migrations/versions/0001_v2_identity_org.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/db/rls.py
unit-tests:
  - backend/tests/unit/auth/test_api_key.py
  - backend/tests/unit/api/test_api_keys_endpoint.py
  - backend/tests/unit/api/test_error_handling.py
depends-on: [feat-teams-team-crud]
status: covered
---
# Team API Keys

Per-org, role-scoped API keys for CI/CD pipelines and external agents, with optional team boundary enforcement.

## Behaviours

### Key format and generation

- [x] Key format is `mk_<8-char-prefix>_<32-char-secret>`
- [x] Full key returned exactly once at creation — never recoverable
- [x] Each call to generate produces a unique key
- [x] `lookup_prefix` (first 8 chars after `mk_`) enables fast DB index lookup
- [x] `lookup_prefix` has a UNIQUE constraint across the org
- [x] SHA-256 hash of the full key stored (`hashed_secret`) — not bcrypt
- [x] Constant-time comparison (`hmac.compare_digest`) used on validation

### Role enforcement

- [x] Valid roles: `operator` and `runner` only
- [x] `admin` role is rejected with 422 on create and update
- [x] `viewer` role is not valid for API keys
- [x] Runner-scoped key: trigger runs and read endpoints only — cannot approve HITL, access connector settings, or modify pipelines
- [x] Operator-scoped key: trigger runs, approve HITL gates (subject to `human_only` and `required_team_id`), and all read endpoints
- [x] Role enforced at the ViewModel command layer (same enforcement path as JWT roles)
- [x] DB CHECK constraint (`ck_org_api_keys_role`) enforces role values at the database level

### Team-scoped API keys

- [x] API key carries optional `team_id` FK to `teams.id` (nullable, CASCADE on team delete)
- [x] Team-scoped API key is restricted to resources accessible to that team under the key's embedded role — MCP tools reject access to pipelines/runs owned by a different team (`team_boundary_violation`)
- [x] Team boundary is enforced across the full pipeline/run/trigger/analytics surface: `trigger_pipeline`, `get_pipeline_graph`, `update_pipeline_graph`, `bind_connector_to_node`, `get_run_status`, `get_run_output`, `get_run_evals`, `cancel_run` (resolve target pipeline/run owner), plus `list_pipelines`, `list_runs`, `list_triggers`, `list_pending_hitl`, `query_analytics`, `query_analytics_concurrency` (team-filtered listing) and `delete_pipeline`, `create_trigger`, `get_trigger`, `update_trigger`, `delete_trigger` (resolve target owner)
- [x] Org-wide API key (NULL `team_id`) respects org-level role only — no team boundary (verified: `_team_scoped_key_mismatch` returns False for `team_id=None`)
- [x] Team-scoped API keys cannot have `admin` role — `_validate_team_key_role` helper enforces this on both create and update
- [x] Admin required to set or update `team_id` on create/PUT (403 for non-admin)
- [x] `team_id` is serialised in list/response payloads

### CRUD lifecycle

- [x] POST `/api/v1/api-keys` creates a key with name, role, optional team_id, optional expires_at
- [x] GET `/api/v1/api-keys` lists active keys (or all including revoked) for the org
- [x] PUT `/api/v1/api-keys/{key_id}` updates name, role, team_id, expires_at
- [x] DELETE `/api/v1/api-keys/{key_id}` sets `revoked_at` (soft-delete)
- [x] GET `/api/v1/api-keys/mcp-config` returns MCP URL and Claude Desktop / Cursor config snippet
- [x] All API key management endpoints set RLS org context (`set_rls_org`) and user context (`set_rls_user_context`)
- [x] API key not found returns 404 on update/revoke
- [x] Updates to `team_id` require admin privilege (403 otherwise)
- [x] Roles restricted to `operator`/`runner` on update — `admin` returns 422
- [x] Key name minimum length 1 character

### Validation

- [x] Validation checks prefix (`mk_`), org_id match, not revoked, not expired, hash match
- [x] Expired key returns `ApiKeyInvalidError`
- [x] Revoked key returns `ApiKeyInvalidError`
- [x] Key from wrong org returns `ApiKeyInvalidError`
- [x] Hash mismatch returns `ApiKeyInvalidError` (constant-time compare)
- [x] `last_used_at` updated on every successful validation
- [x] `expires_at` and `revoked_at` are nullable — permanent keys if both are null

### MCP auth integration

- [x] MCP middleware (`McpAuthMiddleware`) accepts API key bearer tokens with `mk_` prefix
- [x] Validated key's `role` stored in `_ctx_role` ContextVar for tool handlers
- [x] Validated key's `key_id` stored in `_ctx_key_id` ContextVar
- [x] Validated key's `team_id` stored in `_ctx_team_id` ContextVar — set on API-key auth (and refreshed per-event on SSE re-validation); reset to `None` for OAuth/JWT tokens
- [x] OAuth 2.0 access tokens are the fallback auth mechanism (checked after API key)
- [x] Health check endpoint (`/mcp/healthz`) is exempt from auth

### Team gating

- [x] Team RBAC toggle controls whether team-scoped API keys are usable

## Error Handling

### Database errors

- [x] All 4 DB-accessing route handlers wrap queries in `try/except ProgrammingError` → 501 Not Implemented
- [x] `ProgrammingError` catch returns structured JSON with `detail` explaining the migration requirement
- [x] Route handlers DO catch `SQLAlchemyError` for general DB failures — returns 503 Service Unavailable
- [x] Route handlers DO catch generic `Exception` for Python-level errors (`TypeError`, `AttributeValue`) — returns 500 Internal Server Error

### API key validation errors

- [x] Invalid prefix → `ApiKeyInvalidError` (401)
- [x] Key not found in DB → `ApiKeyInvalidError` (401)
- [x] Expired key → `ApiKeyInvalidError` (401)
- [x] Revoked key → caught by "not found" filter (query excludes revoked)
- [x] Hash mismatch → `ApiKeyInvalidError` (401, constant-time compare)
- [x] Missing bearer token → 401 raised by `get_current_user` dependency

### Input validation errors

- [x] Empty key name (length < 1) → 422 from Pydantic `Field(min_length=1)`
- [x] Invalid role (not `operator`/`runner`) → 422 from route handler guard
- [x] Invalid `team_id` format (not a valid UUID) → 422 from `uuid.UUID()` conversion
- [x] Invalid `expires_at` format (not ISO 8601) → 422 from `datetime.fromisoformat()`
- [x] Team-scoped key with admin role → caught at route level before service layer (422)

## Resilience

### Startup / migration readiness

- [x] Missing `org_api_keys` table → 501 from all route handlers (ProgrammingError catch)
- [x] `lookup_prefix` UNIQUE constraint prevents prefix collision at DB level
- [x] Missing DB or connection failure IS caught separately from ProgrammingError — all four DB-accessing handlers also catch `SQLAlchemyError` → 503 (verified in `api/routes/api_keys.py` create/list/update/revoke + `test_error_handling.py`)

### Runtime resilience

- [x] `last_used_at` update failure does not block key validation — update is fire-and-forget via `session.execute()`
- [x] `last_used_at` is nullable — no data loss if migration hasn't added the column
- [x] `revoked_at` is nullable — permanent keys work without it
- [x] `expires_at` is nullable — non-expiring keys work without it
- [x] Constant-time comparison (`hmac.compare_digest`) prevents timing side-channels
- [x] Session rollback on `ProgrammingError` — each handler scopes its work in `async with session.begin():` (rolls back on exception) and the DI session is request-scoped, so a failed transaction never leaks stale state into a subsequent request

### Team-scoped resilience

- [x] `_validate_team_key_role` fires BEFORE `session.add()` in `create_api_key` (and before mutation in `update_api_key`) — a validation failure never leaves a partially-initialised key object in the session

## Edge Cases

### Key lifecycle edge cases

- [x] Create key with `team_id` for a non-existent team → FK violation → 409 (IntegrityError catch — covered by `test_create_api_key_with_unknown_team_returns_409`)
- [x] Create key with `expires_at` in the past → 422 `expires_at must be in the future` (validation added 2026-08-06)
- [x] Update key with `expires_at` in the past → 422 `expires_at must be in the future` (validation added 2026-08-06)
- [x] Update revoked key → returns 404 (query filters `revoked_at.is_(None)`)
- [x] Re-revoke an already-revoked key → 404 (query filters `revoked_at.is_(None)`)
- [x] Key name with leading/trailing whitespace → stripped via `_normalise_name` before create/update (2026-08-06); whitespace-only names rejected with 422
- [x] `lookup_prefix` of exactly 8 chars in the DB model — `OrgApiKey.lookup_prefix` is `String(8)`, any shorter/longer prefix cannot match the index (verified by `test_lookup_prefix_column_is_string_8`)
- [x] MCP middleware validates API key without `org_id` → resolves org from the key's `organisation_id` column and sets `_ctx_team_id` from the key's `team_id` (2026-08-12)

### Team-scoped edge cases

- [x] Team-scoped key with admin role raises `ApiKeyInvalidError` in `_validate_team_key_role`
- [x] `_validate_team_key_role` fires on BOTH create and update when `team_id` is non-None
- [x] Updating a key from org-wide (team_id=None) to team-scoped → `_validate_team_key_role` validates the effective (new) team scope before the mutation is applied (covered by `test_update_api_key_with_team_id_rejects_admin` + `test_update_api_key_updates_team_id`)
- [x] Updating a key from team-scoped to org-wide (team_id=None) → allowed and now implemented (2026-08-15): PUT with `team_id: null` clears the scope (admin-only); the absence of a `team_id` key in the payload leaves the scope unchanged via the `_UNSET` sentinel — covered by `test_update_api_key_clears_team_id` / `test_update_api_key_team_id_unset_leaves_scope_unchanged` (auth) and `test_update_api_key_clears_team_id` / `test_update_api_key_without_team_id_passes_unset_sentinel` / `test_update_api_key_clear_team_requires_admin` (endpoint)
- [x] Team deletion cascades to `team_id` via `FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE` — team-scoped keys are DELETED (not silently widened to org-wide) when the team is deleted; deleting the keys is the safe behaviour (widening scope would leak the team's boundary)

### Concurrency edge cases

- [x] `revoke_api_key` now takes a `FOR UPDATE` row lock on the key row before setting `revoked_at` (2026-08-15) — concurrent validation/revocation can no longer race on the read-then-update; the `last_used_at` write in `validate_api_key` stays fire-and-forget (best-effort, not revocation semantics)
- [x] Two concurrent revocations for the same key serialise on the row lock — the second re-reads with `revoked_at` set, is excluded by the `revoked_at IS NULL` filter, and returns False (a clean no-op) instead of a racing second write

## Known Gaps

- **BDD coverage is incomplete.** Feature file at `backend/tests/bdd/features/auth/api_keys.feature` has 5 real scenarios (happy path, create, list, revoke, invalid, reject) but is missing scenarios for: admin role rejection, team-scoped key creation, MCP auth validation, role-scope enforcement, MCP config endpoint, not-found handling, unauthenticated access, soft-delete revocation.
- **No RLS policy on `org_api_keys` table for team isolation.** The MCP/API-key list endpoint filters by `organisation_id` only, not by the requesting key's `team_id`. Mitigation: the list endpoint requires `api_key.update` (admin-level) permission and team-scoped keys cannot be `admin` (enforced by `_validate_team_key_role`), so a team-scoped key cannot reach the endpoint — the theoretical enumeration path is closed by role, not by RLS.

## QA History

### 2026-08-15 — improve-architecture (drive team-api-keys → covered, FAR-244)
- **IMPLEMENTED team-scope clearing on update** (line 158) — PUT with `team_id: null` now moves a team-scoped key back to org-wide (admin-only; the `_UNSET` sentinel distinguishes "not provided" from "clear"). `update_api_key` accepts `_UNSET`/`None`/UUID and validates the effective (new) team scope before mutating. Covered by 3 auth-layer + 3 endpoint tests.
- **IMPLEMENTED row-lock on revocation** (lines 163/164) — `revoke_api_key` now selects the key with `FOR UPDATE`, so two concurrent revocations serialise: the second re-reads `revoked_at` set, is excluded by the `revoked_at IS NULL` filter, and returns False instead of a racing write. Covered by `test_revoke_api_key_select_locks_row_for_update`.
- **VERIFIED [ ]→[x] resilience/edge boxes** — 125 (SQLAlchemyError→503 IS caught on all 4 handlers), 134 (per-request `async with session.begin()` rolls back on failure; no stale session state), 138 (`_validate_team_key_role` fires before `session.add` in create and before mutation in update), 144 (non-existent team → FK violation → 409; endpoint test added), 150 (`lookup_prefix` is `String(8)`; column-length test added), 159 (FK is `ondelete="CASCADE"` so team deletion DELETES its keys — the prior "keys become org-wide" expectation was wrong; CASCADE is the safe behaviour).
- **BDD coverage gap re-evaluated** — the existing `api_keys.feature` scenarios exercise mock-based step definitions in `test_auth.py`; the missing scenarios in the Known Gap (admin-role rejection, team-scoped creation, MCP auth, role-scope enforcement) would require real-DB step behaviour that the mock steps cannot produce correctly, so they are tracked as a Known Gap rather than added half-working. The behaviours themselves are covered by the unit/endpoint suites listed above.
- **Known gaps still open:** BDD coverage incomplete (above); no RLS policy on `org_api_keys` for team isolation (mitigated by role: list endpoint requires `api_key.update` admin-level permission and team-scoped keys cannot be admin).

### 2026-08-12 — improve-architecture (product-map walk, index 171)
- **RESOLVED "MCP middleware does not propagate `team_id` to request context"** — added `_ctx_team_id` ContextVar (`mcp_server.py`). `McpAuthMiddleware` sets it from the validated key's `team_id` on API-key auth (both initial dispatch and per-event `validate_current_auth` SSE re-validation); the OAuth/regular-JWT paths explicitly reset it to `None` so user tokens carry no team boundary. Exposed via `_ctx_team_id_val()`.
- **Implemented team-boundary enforcement at the MCP tool layer** — new `_team_scoped_key_mismatch(owner_team_id)` + `_team_scope_error(...)` helpers; `_pipeline_owner_team_id(session, pipeline_id)` resolves a resource's owning team. A team-scoped key is blocked with error `team_boundary_violation` on pipelines owned by a different team (and org-level pipelines / own-team pipelines remain accessible) across `trigger_pipeline`, `get_pipeline_graph`, `update_pipeline_graph`, `bind_connector_to_node`, `get_run_status`, `get_run_output`, `get_run_evals`, and `cancel_run`. Org-wide keys (`team_id=None`) and OAuth/JWT callers are unaffected — no team boundary.
- **RESOLVED "No team-scoped enforcement unit tests"** — added `tests/unit/mcp/test_team_scope_enforcement.py` (33 tests): helper semantics (`_team_scoped_key_mismatch` matrix, `_ctx_team_id_val` default, error dict shape), trigger blocked/own-team-allowed/org-wide-allowed, graph read blocked/org-wide-allowed, run tools blocked for other-team runs (status, output, evals, cancel), plus blocked/allowed cases for `update_pipeline_graph` and `bind_connector_to_node` and coverage of the newly guarded list/delete tools (`list_pipelines`, `list_runs`, `list_triggers`, `list_pending_hitl`, `delete_pipeline`, trigger create/update/delete, `query_analytics`).
- **CLOSED the cross-team read/mutation gap on unguarded tools (2026-08-13)** — `list_pipelines`, `list_runs`, `list_triggers`, `list_pending_hitl`, `query_analytics` and `query_analytics_concurrency` now take a team filter (own-team + org-level resources) when the caller is a team-scoped key; `delete_pipeline`, `create_trigger`, `get_trigger`, `update_trigger` and `delete_trigger` resolve the target pipeline/trigger's owning team and reject with `team_boundary_violation`. Run tools now use `Run.owner_team_id` (snapshot at run creation) as the source of truth instead of resolving through the pipeline's current team assignment, so the boundary cannot drift if a pipeline is re-assigned teams after runs are created.
- **FIXED run/HITL/analytics guard no-op (2026-08-13)** — `Run.owner_team_id` was never populated by any production code path, so every guard that read it (run tools, `list_pending_hitl`, `RunDailyFact.team_id` analytics filter) silently treated cross-team runs as org-level. `create_run` now inherits the pipeline's `owner_team_id` when no explicit team is passed, stamping the run (and its analytics facts) at creation; for pre-existing NULL-stamped runs the guards fall back to the pipeline's owner team (`_run_owner_team_id`), and the HITL/analytics filters use `COALESCE(Run.owner_team_id, Pipeline.owner_team_id)`. A NULL stamp can no longer widen the boundary.
- **Known gaps still open:** BDD coverage incomplete; RLS team isolation on `org_api_keys` (mitigated by role, documented above).

### 2026-08-06 — improve-architecture (product-map walk)
- Fixed: `expires_at` in the past no longer accepted on create or update — `_parse_expires_at` normalises naive/`Z`-suffixed datetimes to UTC-aware and the route returns 422 `expires_at must be in the future`.
- Fixed: API key names are stripped of surrounding whitespace via `_normalise_name` before create/update; whitespace-only names rejected with 422. Added 6 endpoint unit tests covering past-expiry rejection (create + update), whitespace-only name rejection, and name trimming.

### Index 135 — 2026-07-04
- Fixed CRITICAL: `test_create_api_key_accepts_expires_at` passed `created_by=user_id` instead of `account_id=user_id` — would raise TypeError
- Added `test_api_keys_programming_error.py` with 4 unit tests covering all DB-accessing route handlers (create, list, update, revoke → 501)
- Stale `expires_at` gap corrected: PUT route DOES parse and propagate `expires_at`, but lacks dedicated test
- `_validate_team_key_role` wired to both create and update routes — team-scoped keys with admin role now caught at route level (422) and by validation function

### 2026-07-05 — QA-iterate (prodmap auth)
- Moved `_validate_team_key_role` fix note from Known Gaps to QA History

### 2026-07-11 — Round 2 re-QA (feat-auth-team-api-keys, index 360)
- Fixed: removed unnecessary `IntegrityError` catch from `list_api_keys_endpoint` (read-only endpoint cannot produce IntegrityError)
- Fixed: removed misleading `test_validate_api_key_revoked_raises` — it tested hash mismatch, not revocation (revocation is handled by SQL WHERE filter, tested by `test_validate_api_key_not_found_raises`)
- Fixed: product map Known Gap #5 outdated — `expires_at` update now has both unit and endpoint test coverage
- Verified: remaining 4 major Known Gaps still valid (MCP team_id propagation, BDD coverage, team-scoped enforcement tests, RLS team isolation)
- Verified: all 13 unchecked edge cases remain unchecked and correctly documented as gaps

### 2026-07-05 — Cross-cutting QA (feat-auth-team-api-keys)
- Added Error Handling, Resilience, and Edge Cases sections to product map
- Added 10 new unit tests for `_validate_team_key_role`, team-scoped create/update, `expires_at` update, and revoked key validation
- Added 3 new endpoint tests for `expires_at` update, empty name rejection on create and update
- Fixed pre-existing test bug: `ApiKeyCreatedResponse` field is `key_value`, not `full_key` — 4 tests were asserting `body["full_key"]` which always failed with `KeyError`
- Website docs stub not created — `Website` is a separate git repo, not part of this worktree
- Product map now has structured Error Handling, Resilience, and Edge Cases sections with 44 checkboxes covering both covered and gap behaviours
