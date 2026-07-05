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
  - backend/src/modulo/db/migrations/versions/0001_initial_schema.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/db/rls.py
unit-tests:
  - backend/tests/unit/auth/test_api_key.py
  - backend/tests/unit/api/test_api_keys_endpoint.py
  - backend/tests/unit/auth/test_api_keys_programming_error.py
depends-on: [feat-teams-team-crud]
status: partial
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
- [ ] Team-scoped API key is restricted to resources accessible to that team under the key's embedded role
- [ ] Org-wide API key (NULL `team_id`) respects org-level role only — no team boundary
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
- [x] OAuth 2.0 access tokens are the fallback auth mechanism (checked after API key)
- [x] Health check endpoint (`/mcp/healthz`) is exempt from auth

### Enterprise gating

- [x] Team RBAC toggle controls whether team-scoped API keys are usable

## Error Handling

### Database errors

- [x] All 4 DB-accessing route handlers wrap queries in `try/except ProgrammingError` → 501 Not Implemented
- [x] `ProgrammingError` catch returns structured JSON with `detail` explaining the migration requirement
- [ ] Route handlers do NOT catch `SQLAlchemyError` for general DB failures (integrity, connection) — these propagate as 500
- [ ] Route handlers do NOT catch generic `Exception` for Python-level errors (`TypeError`, `AttributeValue`) — also propagate as 500

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
- [ ] Team-scoped key with admin role → raised as `ApiKeyInvalidError` at the `api_key.py` layer; route-level guard catches before service layer is reached

## Resilience

### Startup / migration readiness

- [x] Missing `org_api_keys` table → 501 from all route handlers (ProgrammingError catch)
- [x] `lookup_prefix` UNIQUE constraint prevents prefix collision at DB level
- [ ] Missing DB or connection failure is NOT caught separately from ProgrammingError

### Runtime resilience

- [x] `last_used_at` update failure does not block key validation — update is fire-and-forget via `session.execute()`
- [x] `last_used_at` is nullable — no data loss if migration hasn't added the column
- [x] `revoked_at` is nullable — permanent keys work without it
- [x] `expires_at` is nullable — non-expiring keys work without it
- [x] Constant-time comparison (`hmac.compare_digest`) prevents timing side-channels
- [ ] Session rollback on `ProgrammingError` may leave stale session state — no explicit `session.rollback()` after the exception

### Team-scoped resilience

- [ ] `_validate_team_key_role` is called AFTER the key is constructed and added to the session — on failure, the session has a partially-initialised key object that may need rollback

## Edge Cases

### Key lifecycle edge cases

- [ ] Create key with `team_id` for a non-existent team → FK violation → 500 (not caught by ProgrammingError)
- [ ] Create key with `expires_at` in the past → accepted (no validation that `expires_at > now`)
- [ ] Update key with `expires_at` in the past → accepted (no validation that `expires_at > now`)
- [ ] Update revoked key → returns 404 (query filters `revoked_at.is_(None)`)
- [ ] Re-revoke an already-revoked key → 404 (query filters `revoked_at.is_(None)`)
- [ ] Key name with leading/trailing whitespace → stored as-is (no `.strip()`)
- [ ] `lookup_prefix` of exactly 8 chars in the DB model — any shorter/longer prefix fails to match (DB column is `String(8)`)
- [ ] MCP middleware validates API key without `org_id` → resolves org from the key's `organisation_id` column, but `_ctx_team_id` is never set

### Team-scoped edge cases

- [x] Team-scoped key with admin role raises `ApiKeyInvalidError` in `_validate_team_key_role`
- [x] `_validate_team_key_role` fires on BOTH create and update when `team_id` is non-None
- [ ] Updating a key from org-wide (team_id=None) to team-scoped → `_validate_team_key_role` fires after setting `team_id`
- [ ] Updating a key from team-scoped to org-wide (team_id=None) → allowed (no validation needed)
- [ ] Team deletion cascades to `team_id` via `FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE` — keys become org-wide on team deletion

### Concurrency edge cases

- [ ] No row-level locking (`FOR UPDATE`) on read-then-update in `validate_api_key` or `revoke_api_key` — potential race between concurrent validation and revocation
- [ ] Two concurrent revocations for the same key — both read `revoked_at IS NULL`, both proceed; second write is a no-op

## Known Gaps

- **MCP middleware does not propagate `team_id` to request context.** The `_ctx_team_id` ContextVar does not exist — tool handlers have no way to know which team scope an API key was issued for. Team-scoped enforcement at the MCP layer is incomplete.
- **BDD coverage is incomplete.** Feature file at `backend/tests/bdd/features/auth/api_keys.feature` has 5 real scenarios (happy path, create, list, revoke, invalid, reject) but is missing scenarios for: admin role rejection, team-scoped key creation, MCP auth validation, role-scope enforcement, MCP config endpoint, not-found handling, unauthenticated access, soft-delete revocation.
- **No team-scoped enforcement unit tests.** `test_api_key.py` does not test validation of team-scoped keys — no tests verify that a team-scoped key cannot access resources outside its team boundary.
- **No RLS policy on `org_api_keys` table for team isolation.** When querying API keys via MCP, a team-scoped key could theoretically enumerate org-wide keys via the list endpoint — the list endpoint filters by `organisation_id` only, not by the requesting key's `team_id`.
- **`update_api_key` endpoint now supports `expires_at` on update** but lacks test coverage for this path.

## QA History

### Index 135 — 2026-07-04
- Fixed CRITICAL: `test_create_api_key_accepts_expires_at` passed `created_by=user_id` instead of `account_id=user_id` — would raise TypeError
- Added `test_api_keys_programming_error.py` with 4 unit tests covering all DB-accessing route handlers (create, list, update, revoke → 501)
- Stale `expires_at` gap corrected: PUT route DOES parse and propagate `expires_at`, but lacks dedicated test
- `_validate_team_key_role` wired to both create and update routes — team-scoped keys with admin role now caught at route level (422) and by validation function

### 2026-07-05 — QA-iterate (prodmap auth)
- Moved `_validate_team_key_role` fix note from Known Gaps to QA History

### 2026-07-05 — Cross-cutting QA (feat-auth-team-api-keys)
- Added Error Handling, Resilience, and Edge Cases sections to product map
- Added 10 new unit tests for `_validate_team_key_role`, team-scoped create/update, `expires_at` update, and revoked key validation
- Added 3 new endpoint tests for `expires_at` update, empty name rejection on create and update
- Fixed pre-existing test bug: `ApiKeyCreatedResponse` field is `key_value`, not `full_key` — 4 tests were asserting `body["full_key"]` which always failed with `KeyError`
- Website docs stub not created — `Website` is a separate git repo, not part of this worktree
- Product map now has structured Error Handling, Resilience, and Edge Cases sections with 44 checkboxes covering both covered and gap behaviours
