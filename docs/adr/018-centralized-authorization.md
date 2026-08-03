# ADR 018 — Centralized Authorization: Shared Permission Registry for REST + MCP

**Date**: 2026-07-31
**Status**: v9 — revised after 7 plan-review-iterate cycles. **Iterations 6 and 7 both returned zero Criticals** (4 of the last 5 iterations clean on criticals; iteration 5's 2 criticals confirmed fixed). Iteration 7 flagged that several remaining items are implementation seams real tests will surface, not design holes. v9 resolves the iteration-7 majors: CSRF exemption keeps audited parameterized prefixes (a pure exact-path switch would 403 webhook/login in prod); kill-switch mechanism corrected (`jsonb_set` cannot target a `JSON` column — dedicated boolean column instead); B collapses to a single PR (the per-resource split's security value didn't hold and its allowlist was the false-negative hazard AC1 exists to remove); consent UX deferred to minimal-correct (approve POST IS the consent — no consent page/deny affordance for zero users); alias-removal consumer inventory completed (Remy runtime + frontend + 6 more test files + PRD ~12 refs). **Declared converged: the design is stable; remaining work is implementation with the documented test strategy.**
**Implementation tracking**: `task-authz-permission-model` (L), `task-authz-centralized-enforcement` (XL), `task-authz-owner-alias-fix` (S)

---

## Context

Modulo has two API surfaces — FastAPI REST and the MCP server — that expose overlapping operations but enforce authorization completely differently:

- **MCP** has a per-tool scope matrix (`backend/src/modulo/core/mcp/scope_validator.py`): `TOOL_SCOPE_REQUIREMENTS` maps tool → minimum org role; `check_tool_scope()` compares against `ORG_ROLE_HIERARCHY` (viewer 0 < runner 1 < operator 2 < admin 3).
- **REST** uses `get_current_tenant_user` (`backend/src/modulo/auth/dependencies.py`), which verifies authentication + org membership only — **no role comparison**. Any org member, including `viewer`, can call every mutating endpoint.

Verified consequences (2026-07-31 audit + 7 review cycles):

1. **REST/MCP asymmetry**: same op requires `operator`/`runner` over MCP but nothing over REST. An agent holding any user JWT bypasses MCP scope via REST.
2. **HITL gate config weakenable by any org member** (see `hitl-gate-removal-guard-plan.md`).
3. **`owner` is an accidental orphan role**: in the DB CHECK, absent from `ORG_ROLE_HIERARCHY` (`org_role_level("owner") = -1`).
4. **FIVE MCP tools enforced by nothing**: `delete_connector`, `create_secret`, `delete_secret`, `list_secrets`, `list_trigger_events` — absent from `TOOL_SCOPE_REQUIREMENTS`, which **defaults to allow**.
5. **API-key role escalation**: any org member can create an `operator`-role key; MCP middleware trusts the stored role (`backend/src/modulo/api/mcp_server.py:293`).
6. **OAuth protocol flow is anonymous + non-standard**: anonymous authorize, no account binding, role from scopes, stateless 30-day refresh, PKCE unvalidated, no `account_id` on codes/tokens; `/token`/`/refresh` read `request.json()` only (RFC 6749 requires form-urlencoded).
7. **Team RBAC dead code**; DB trigger forbids `team_role > org_role`; RLS team-visibility = **membership (any role) OR org-role='admin'**; RLS policies are **`USING`-only, no `WITH CHECK`** — INSERT is unrestricted at the DB layer.
8. **Live-role staleness**: JWT claim role; refresh copies claims verbatim; `_verify_identity` never queries `org_memberships`.
9. **Middleware/CSRF facts**: `CsrfMiddleware` wraps the `/mcp` mount, exempts prefixes `/api/v1/health,/api/v1/triggers,/api/v1/auth` (parameterized routes live under them — webhook receive/replay/cleanup, login/refresh), skips Bearer requests entirely; tests run with `MODULO_CSRF_ENABLED=false`; `modulo_session` cookie set but never read; OAuth protocol handlers call `_get_session_factory()` directly (no DI) and 500 without `modulo_public_url`.
10. ~~**THREE deprecated MCP tool aliases**: `get_trigger_events` (`backend/src/modulo/api/mcp_server.py:1410-1423`), `browse_library` (`backend/src/modulo/api/mcp_server.py:1338-1351`), `get_documentation` (`backend/src/modulo/api/mcp_server.py:2092-2100`) — plus module-level alias assignments at `backend/src/modulo/api/mcp_server.py:2592-2594`. **Remy's runtime prompt advertises `get_documentation`** (`backend/src/modulo/core/remy/skill_loader.py:34`); frontend references exist (`frontend/src/views/AdminRemyView.vue:834`, `frontend/src/locales/en-US.js:949`); PRD documents the alias-preservation contract (~12 refs incl. `docs/prd.md:540-542`).~~ **[RESOLVED — REMOVED 2026-07-31 in PR #434 (A1a implementation). Audit 2026-08-03 confirms none of the three names remain anywhere in `backend/src` — the tools are registered as `search_library`/`list_trigger_events`/`search_documentation` (see `backend/tests/unit/test_mcp_structural_coverage.py` `_EXPECTED_TOOLS`); `backend/src/modulo/core/remy/skill_loader.py:34` now advertises `search_documentation`; the frontend and PRD alias references no longer exist.]**
11. **`review_hitl:deliver_manual` is impossible via MCP today**: action accepted (`backend/src/modulo/api/mcp_server.py:1192`) but missing from `TOOL_SCOPE_REQUIREMENTS` → "Unknown action" → `insufficient_scope` for all roles.
12. **`organisations.settings_json` is `sa.JSON`, not JSONB** (`backend/src/modulo/db/models/organisation.py:26`) — `jsonb_set` cannot target it without a cast; `get_effective_setting` (`backend/src/modulo/db/settings_resolver.py:13-35`) falls through to process-global `SystemConfig` (no `organisation_id`); there is **no parent-app auth middleware** (only the MCP sub-app's `McpAuthMiddleware`); `validate_api_key` flushes `last_used_at` (`backend/src/modulo/auth/api_key.py:144-145`); `validate_current_auth` never re-sets `_ctx_role`.

## Founder Decisions (resolved 2026-07-31 — "do the right fix regardless of scope")

1. **OAuth: browser consent flow + PKCE** (no users — no legacy migration). Machine clients use org API keys. Iteration-7 refinement: **minimal-correct v1** — approve POST IS the consent; a dedicated consent page + deny affordance is deferred until an interactive customer exists.
2. **Team-scoping: no capability regression.** Effective role = org-role floor; **membership gate carries the org-admin bypass** (RLS parity).
3. **Kill switch: tenancy-bounded** — per-org only, hard global default, NO SystemConfig fallback, dedicated boolean column.
4. **API-key: role-cap; viewers cannot mint keys.**

## Role Model Decision (relitigated 2026-07-31)

Keep 4 org roles (viewer < runner < operator < admin) + 3 team roles (viewer/runner/operator; admin is org-only). **DROP `owner` entirely**:

- **One Alembic revision, one transaction**: `UPDATE org_memberships SET role='admin' WHERE role='owner'`, drop/re-add CHECK as `('admin','operator','runner','viewer')`. Idempotent. Pre-migration row-count audit log. Downgrade = documented no-op.
- **Migration-failure handling**: entrypoint stays non-fatal; `_run_migrations` (currently a no-op) becomes the **single authoritative runner** with a bounded retry loop and **FATAL exhaustion INSIDE the function** (it is called bare at `backend/src/modulo/api/main.py:684` — the bounds must not depend on the caller), guarded by the Postgres advisory-lock pattern. **Hard-fail owner-rows assertion runs AFTER the retry loop** (actionable message listing offending `account_id`s + prescribed UPDATE); `SQLAlchemyError` from the assertion query → log + continue (invariant guaranteed by the migration transaction). **Avoid the double-run crash-loop**: `_run_migrations` must tolerate the entrypoint's prior `alembic upgrade heads` (advisory-lock wait sized against concurrent-machine lock waits; dedicated test). **`infra_blocked` producer defined** (iteration-7: no consumer pipeline exists today — `list_pipelines` shows no Deploy Agent; wiring a consumer + Branch Fixer prompt branch for `infra_blocked` is a follow-up task, not a blocker for this ADR).
- **JWT live-role re-read**: centralized `resolve_role_from_membership(session, account_id, organisation_id)` in **`backend/src/modulo/auth/dependencies.py`** (beside `_verify_identity`; lazy-import pattern). Used by: `get_current_tenant_user`, MCP JWT middleware + SSE revalidation (TTL-bounded ONLY on SSE), `/refresh` (same transaction as `advance_sequence`), OAuth token/refresh/per-call clamp, API-key per-call clamp. Filters **`deactivated_at IS NULL`**. Failure modes: missing/deactivated → **401**; `SQLAlchemyError` → **degrade-to-claim + counter** (REST) / **deny** (MCP); other → 500. `get_current_tenant_user_optional` **stays claim-only** (documented webhook impact). Query budget: piggyback the kill-switch read onto the membership read.
- **No deprecation window**: bare `owner` JWT claim denied at -1 everywhere.
- **8-site cleanup + ORM + docs** (verified complete): `backend/src/modulo/api/routes/pipelines.py:1419`, `backend/src/modulo/api/routes/admin.py:1644` (preserve `is_system_admin` bypass), `backend/src/modulo/api/routes/contributions.py:176`, `backend/src/modulo/cli/migrate.py:80,101` (live-check-only), `backend/src/modulo/core/error_tracking/alert_dispatcher.py:139`, `backend/src/modulo/api/routes/pipelines.py:942` log, ORM model `backend/src/modulo/db/models/org_membership.py:14` + assertion `"owner"` NOT in the model check, docs (PRD §9.2 intro + §9.3.1, product-map org-entity/pipeline-diff-rollback, penetration-test-plan). Semgrep/pattern check: no route checks the literal string `"owner"` as a role.
- **BDD reconciliation**: `/api/` vs `/api/v1/` prefixes; rewrite fabricated steps; add `runner_client`/`operator_client` fixtures; runner-trigger via runner-JWT; "Runner role is scoped to pipelines they own" deferred to Phase 3; per-scenario flip table.

## Centralized Enforcement Design

One permission registry + one check function, REST and MCP as thin adapters. **The registry is the single source of truth; MCP tool requirements reference it, not duplicate it.**

```
PERMISSIONS: dict[str, str]   # "resource.operation"
  "pipeline.graph.update": "operator"
  "run.trigger":             "runner"
  "connector.create":        "operator"
  "connector.delete":        "operator"   # NEW (was unguarded)
  "secret.manage":           "operator"   # NEW (was unguarded)
  "trigger.events.list":     "runner"     # NEW (was unguarded; list_trigger_events)
  "api_key.create":          "runner"     # DECISION 4
  "api_key.update":          "runner"     # DECISION 4
  "metrics.ingest":          "viewer"     # NEW (telemetry)
  "oauth.client.create":     "operator"   # NEW (matches existing gate)
  "oauth.client.list":       "operator"   # NEW (GET /clients currently ungated)
  "trigger.cleanup":         "runner"     # NEW (webhook cleanup-expired)
  ...
  class PermissionDenied(Exception)
  def assert_org_role(role, required, subject)
  def resolve_required(permission) -> str
```

### Registry: organised by resource

Resource-prefixed dot-separated keys. Rationale: future customisable granular RBAC (custom roles = `frozenset[str]`). Deliberately minimal.

**Single-source-of-truth**: `TOOL_SCOPE_REQUIREMENTS` becomes `tool -> permission-key` referencing `PERMISSIONS`. **`_VALID_ROLES` loop rewritten**: resolve each tool's permission key through `PERMISSIONS` (raise on unknown), assert resolved role in `ORG_ROLE_HIERARCHY`. **Delete dead key + remove ALL THREE deprecated aliases** — including the module-level alias assignments at `backend/src/modulo/api/mcp_server.py:2592-2594` and the `backend/src/modulo/api/mcp_server.py:2978` docstring. **Complete consumer inventory (iteration-7)** **[IMPLEMENTED — PR #434, 2026-07-31: dead key deleted, aliases removed, all consumers below migrated]**:
- Production: `backend/src/modulo/core/remy/skill_loader.py:34` (prompt advertises `get_documentation` → switch to `search_documentation`), `frontend/src/views/AdminRemyView.vue:834` (`toolCall: 'get_documentation()'`), `frontend/src/locales/en-US.js:949` (`browse_library` key).
- Tests: `tests/unit/mcp/test_get_trigger_events.py` (entire file), `tests/unit/mcp/test_browse_library.py`, `tests/unit/remy/test_context_tools.py`, `tests/unit/remy/test_skill_loader.py:452`, `tests/bdd/steps/test_remy_context_sources.py:167`, `tests/bdd/steps/test_personas.py:770`, `tests/bdd/steps/test_alpha_triggers.py:258`, `tests/bdd/features/onboarding/sdlc_onboarding.feature:42,45`, `tests/bdd/features/remy/remy_context_sources.feature`, `test_sdlc_onboarding.py:17`, `test_mcp_security.py:44` (migrate `("viewer","get_trigger_events")` → `list_trigger_events` runner), **`test_mcp_security.py:55-70`** (unrestricted-tools parametrization — `list_pipelines_tool` unmapped today → allow; under deny-by-default it fails; migrate to the real tool names + pin `get_run_status` at viewer), `test_scope_validator.py:209` (dead key).
- Docs: PRD ~12 refs (`docs/prd.md:530,:532,:540-542,:1947,:2639,:2664,:2681,:2688,:2766,:2812,:2822` — incl. the alias-preservation contract that the removal overturns), `docs/alpha.md:144`.
The PRD's alias-preservation contract is deliberately superseded by this ADR; update the PRD in the same PR.

**MCP structural coverage**: iterate the FastMCP tool registry; every **mutating** tool must be in `TOOL_SCOPE_REQUIREMENTS` or on an explicit read-only allowlist (read-only tools pinned at `viewer`). Pin the `mcp` package; assert the tool-name set is **equal** to a fixed list. **Allow→deny flip stated explicitly**: unmapped mutating tools FAIL; unmapped read-only tools pinned at `viewer`.

**`review_hitl:deliver_manual` @ operator added**.

### Exception contract

- `assert_org_role` raises shared **`PermissionDenied(Exception)`** with `permission`, `required_role`, `actual_role`.
- **REST**: dependency catches → `HTTPException(403, detail=...)`.
- **MCP**: `check_tool_scope` catches at its boundary → `MCPAuthorizationError` (subclass or wrap), preserving all 26 handlers and `insufficient_scope`.
- **Fail-closed**: unknown role, empty string, `None` → deny.
- **Import-time key validation**: `resolve_required` inside the factory at import.

### REST adapters — five semantics, no fall-through

1. `require_permission(permission)` — tenant org-role.
2. `require_system_permission(permission)` — strict `is_system_admin`-only (NO org-role fall-through — license-gate bypass).
3. `require_system_or_org_admin(permission)` — the one true hybrid: `backend/src/modulo/api/routes/admin.py:1641` (org deletion).
4. `require_target_org_role(permission, min_role)` — **reads** variant (`org.email.view`/`org.license.view` @ operator; renamed from `admin.*.view` to avoid over-grant in the custom-role model). **Composes `is_system_admin OR target-org role >= min_role`** (iteration-7: today a non-member system admin gets 200 on these reads — the membership-only reading would regress them; add an AC for sys-admin without target-org membership).
5. `require_target_org_role(permission, min_role)` — **mutations** variant (`org.email.manage` @ admin). Both use a **live target-org membership lookup filtering `deactivated_at IS NULL`** (shared with `resolve_role_from_membership`). **Cross-org expansion documented + tested**: member of A+B with current=B → `org.email.view` on A allowed at operator; non-member → denied.

All variants tag `_dep.permission` + `_dep.permission_kind`. Introspection asserts variant-kind + resolved permission key + min role.

### MCP adapter

`check_tool_scope` keeps action-key resolution + explicit allowlist (deny-by-default) — delegates comparison to `assert_org_role`. **Remove `check_tool_scope`'s own `org_role_level`/`<0` guard**. Add 5 missing tools (4 @ `operator`, `list_trigger_events` @ `runner`); add `review_hitl:deliver_manual` @ `operator`; delete dead key + 3 aliases.

### OAuth — minimal-correct browser consent + PKCE (DECISION 1; iterations 4-7)

**Iteration-7 refinement — minimal-correct v1**: the security core is account-bound codes, PKCE S256 validation, form-urlencoded wire format, per-call live-role clamp, exact CSRF handling. **A dedicated consent page + deny affordance is deferred until an interactive customer exists** — the authenticated approve POST IS the consent (the SPA action with a Bearer IS the human approval). Displayed scope/client info is display-only, never authoritative — **approve mints the code from the state row's scopes ONLY** (a display-vs-minted mismatch would be an escalation bug).

**Flow (pinned):**
1. Client redirects browser to `/mcp/oauth/authorize` (GET, sub-app) → **thin 302** to the SPA authorize/consent route (`/oauth/authorize?client_id=...&redirect_uri=...&state=...&code_challenge=...`), anonymous, `Referrer-Policy: no-referrer`. Params validated at authorize (client exists, `redirect_uri` exact-match, S256-only). 302 Location via the existing `_frontend_url` pattern. **Old POST handler + `create_authorization_code` at `backend/src/modulo/api/mcp_server.py:3131` DELETED**; BDD feature rewritten to expect 302.
2. **State store** (`oauth_consent_states` — NEW table, part of migration 2): created at authorize with `{client_id, redirect_uri, scopes, code_challenge, state, org_id, expires_at}`, `expires_at` NOT NULL (TTL ~15 min), single-use via `UPDATE ... WHERE state=:s AND consumed=false RETURNING`, RLS on `organisation_id`. **`account_id` populated at approve** (authorize is anonymous). **Cleanup**: no code-cleanup job exists today (verified — codes are one-time-consumed) — add a TTL sweep to the same periodic job that will sweep expired codes, or state "TTL at read + low volume, no cleanup needed". If the user isn't SPA-logged-in, the consent route re-derives the pending request from the echoed `state` after login (login guard must NOT drop unknown query params).
3. **Approve POST** (`/api/v1/mcp/oauth/consent/approve`) — `get_current_tenant_user` (`auto_error=True`, **no `modulo_session` cookie fallback**). **The Bearer requirement IS the consent-CSRF control** (cross-origin auto-POST cannot attach localStorage Bearer; `CsrfMiddleware` skips Bearer anyway; `state` is client-chosen correlation/replay-binding, NOT an anti-CSRF token). Handler: validates `state` (single-use), re-validates `redirect_uri` **from the state row only** (never client-supplied — a tampered URL would deliver the code to a mismatched URI), checks cross-org (approver's org from Bearer vs state row's org), mints the code bound to `{account_id, code_challenge from the state row, code_challenge_method='S256'}` (so token-time PKCE verification has the original challenge), marks state consumed, returns the **server-derived full redirect URL** (`redirect_uri?code=...&state=...`).
4. Client exchanges at `/mcp/oauth/token` (form-urlencoded per RFC 6749, PKCE verifier, client_secret from body or Basic auth) → access+refresh tokens carrying `account_id`; `/refresh` same.

**CSRF**: `/mcp/oauth/token` and `/mcp/oauth/refresh` authenticate via client_secret-in-body (cookie-independent) — **hardcode their exact paths in `CsrfMiddleware`** as CSRF-exempt (not just env config). **Keep the existing parameterized prefixes `/api/v1/triggers/` and `/api/v1/auth/`** (iteration-7: a pure exact-path switch would 403 webhook receive/replay/cleanup and login/refresh in production — these are HMAC/Bearer-optional POSTs with no CSRF cookie). **Audit the exemption set as a whole**: exact paths for OAuth protocol, audited parameterized prefixes for triggers/auth, each with a documented reason; add a CI assertion that the deployed exemption set matches the committed set; scope the "no exempt prefix shadows a cookie-authenticated mutating route" introspection assertion to cookie-authenticated routes only.

**RFC 6749 wire**: `_oauth_token`/`_oauth_refresh` dispatch on `Content-Type` — form-urlencoded → `request.form()` (incl. `code_verifier` per RFC 7636 §4.5); else JSON for compat; else `invalid_request`. Accept `client_secret` from body AND Basic auth header (matches `check_endpoint_auth_method` accepting `client_secret_basic`). Rewritten tests send form-encoded `data=` bodies.

**Schema migration 2**: `DELETE FROM oauth_authorization_codes` (all rows — anonymous, 10-min TTL, no value), then `account_id` NOT NULL + `code_challenge_method` NOT NULL + **`oauth_consent_states` table**. ORM + `create_authorization_code` change in the SAME PR. **Migration 2 has a trivial downgrade (DROP NOT NULL, DROP table) — A1b is fully revertible** (iteration-7: the "forward-fix-only" framing was inverted; it's A1a's owner-drop that is truly irreversible).

**Per-call live re-validation**: MCP middleware OAuth branch runs `resolve_role_from_membership(claims.account_id)` per call (no TTL on per-request HTTP) and **clamps the scope-derived role to the live role**; SSE per-event revalidation is TTL-bounded and **re-applies the clamp** (`validate_current_auth` must be extended to re-set `_ctx_role`). Demote-then-call test.

**`_ctx_user_id`/`auth_principal` shape**: OAuth branch's synthetic `uuid5(client_id)` → real `account_id`; field set consistent across JWT/API-key/OAuth branches (AGENTS.md lesson — missing field causes `KeyError` in `RateLimitMiddleware._client_key()`).

**Tests**: authorize-GET 302; approve no-session 401; **approve valid state no Bearer 401**; PKCE missing/mismatched denied; `plain`/empty rejected; token-for-scopes-exceeding-live-role denied; demote-then-refresh denied; demote-then-call clamped; wrong `state` denied; **tampered `redirect_uri` at approve denied**; state double-consume denied; state TTL-expired denied; cross-org denied; viewer GET /clients 403; **CSRF-enabled e2e on a fresh `CsrfMiddleware(app, settings=Settings(...))`-wrapped app with a NEGATIVE control** (same non-Bearer POST with empty exemptions → 403, proving the middleware is live) — do NOT enter the wrapped app's lifespan (test_api_contract.py:102 pattern; `_make_settings()` for the strong Settings fields); form-urlencoded with `code_verifier` forwarding asserted (`mock_consume.assert_awaited()` + `call_args.kwargs["code_verifier"] == cv`). Existing OAuth test migration is an **A1b** line item (corrected attribution: `tests/bdd/steps/test_mcp_oauth.py` / `test_mcp_oauth_bdd.py`, ~70 test functions 13+21+45; `@awaiting-implementation` tags removed).

### API-key role-cap (DECISION 4; iterations 5-7)

- `api_key.create`/`update`/`revoke` at `runner`. Viewer cannot mint.
- **Role-cap**: `requested_role_level ≤ caller live org_role_level` on create/update. Admin keys remain prohibited.
- **Live re-validation — DEGRADE, ONE pinned seam (iteration-7)**: use the **pure `_clamp_role(minted, live)` helper** (unit-testable, no DB). `validate_api_key` KEEPS returning the key (its current signature `(session, full_key, org_id=None)` has no live-role input — don't change it); **the middleware resolves the live role, applies `_clamp_role(key.role, live_role)`, and sets `_ctx_role.set(clamped)`**. `key.role` is NEVER mutated (the ORM flushes `last_used_at` — a mutation would persist demotion permanently). `validate_current_auth` consumes the same clamped value.
- **Demotion contract**: demoted operator's key degrades to the live role (CI keeps running at the lower cap); removed/deactivated owner → key **dies** (401). `permission.api_key_role_cap` counter + admin notification threshold + "keys affected" listing (stored role un-lowered, so the listing is accurate).
- **Middleware seam EXCLUSIVELY A2's footprint** (scoped to the API-key branch only — `validate_current_auth` is a shared-edit file flagged in both runbooks).
- **Dispatch-level demote test patch set (iteration-7, pinned)**: patch `_get_session_factory` + `validate_api_key` (returns the key) + `resolve_role_from_membership` (returns `"runner"`); set up the fake key's `hashed_secret` = `_hash_key(token)` (the middleware's raw pre-check at `backend/src/modulo/api/mcp_server.py:261-287` requires it); ContextVar `token.reset()` teardown. Then `check_tool_scope(_ctx_role_val(), "create_pipeline")` raises `MCPAuthorizationError`. Label the true end-to-end demote-then-call as integration/ASGI.
- Team-scoped keys stay org-admin-gated.
- Tests: viewer cannot mint; runner/operator boundaries; `_clamp_role` pure tests; counter; middleware dispatch-level demote test.

### Team-scoping — RLS-parity floor (DECISION 2)

**Gate = `(org_role >= required) AND (visibility='org' OR owner_team_id IS NULL OR membership_any_role OR org_role='admin')`** — membership gate carries the **org-admin bypass** (RLS parity). Team *role* dead code until Phase 3.

**Team-scoped resource set**: `pipelines`, `stages`, `connector_instances`, `model_backends`, `environment_profiles`, `library_primitives`, `lifecycle_maps` (only strict org RLS — membership gate is its only team enforcement). **`runs` special case (iteration-7 pinned): `runs` has strict org RLS + `owner_team_id` with no `visibility` column — pin runs to the org-role floor ONLY (RLS parity); team-scoping of runs arrives with Phase-2 `WITH CHECK`** (the natural `(owner_team_id IS NULL OR membership...)` reduction would regress org members who are not on the owning team — RLS grants all org members read today). Matrix deliverable maps each team-scoped route to its `owner_team_id` source.

**Tests (dependency-level)**: org-operator/team-runner allowed; org-viewer/team-viewer denied; org-viewer/no-membership denied; org-admin/no-membership allowed; `visibility='org'`+`owner_team_id` not team-gated; runs org-role-floor-only. Service-layer test: team-operator removes a gate on a team-scoped pipeline.

### Service-layer backstop

`replace_pipeline_graph`/`rollback_to_snapshot` take `is_privileged` (explicit kwarg, no default); guard **conditional** (only when the write removes/weakens an existing `hitl_gate_config`). Semgrep rule: every call passes `is_privileged=`. `is_privileged` from a flag-independent role check (live-read), NOT the kill-switched `assert_org_role` — HITL guard non-disableable.

### Phase-1 sweep: ALL mutating user-principal endpoints — exact-path exclusions

"All" = every endpoint that (a) mutates state and (b) resolves a user principal. **Exempt list is exact-path (or audited parameterized prefix) with a documented reason per entry.**

| Exact path / channel | Auth model | Handling |
|---|---|---|
| REST routes with `get_current_tenant_user` | user JWT | `require_permission(...)` — the sweep |
| `admin_feature_flags`, `admin_system_config`, `admin_dev_mode`, `admin_orgs` strict | `is_system_admin` | `require_system_permission` strict |
| `backend/src/modulo/api/routes/admin.py` org deletion (5 routes) | sys-admin OR org admin | `require_system_or_org_admin` |
| `GET .../org/{org_id}/email-settings`, `GET .../license` | sys-admin OR target-org member | reads variant: `is_system_admin OR org.email.view` @ operator |
| `PUT .../email-settings`, `POST .../email-settings/test`, `backend/src/modulo/api/routes/admin_orgs.py:419,506` | same, no role min today | mutations variant: `org.email.manage` @ admin |
| `POST /api/v1/triggers/{trigger_id}/webhook` (+ replay, cleanup-expired) | HMAC + timestamp, optional principal | **CSRF-exempt via audited `/api/v1/triggers/` prefix** (kept); sweep handling: receive exempt (shared-secret), **replay fixed to require `runner` role or valid HMAC**, **cleanup-expired swept @ runner** |
| `POST /api/v1/errors/ingest/public` | unauthenticated | Exempt |
| `POST /api/v1/metrics/web-vitals` | tenant principal | Swept with `metrics.ingest: viewer` |
| SCIM (`backend/src/modulo/api/routes/scim.py`) | `MODULO_SCIM_TOKEN` | Exempt at phase 1. `scim_update_user` has a *functional* role-UPDATE — keep unwired (or delete); test no SCIM route sets an org role; document runner-default grant for Phase 3 |
| Cron/scheduled triggers | no principal | Exempt — pipeline-owner access via trigger config |
| OAuth protocol (authorize 302, consent approve, token, refresh) | session + PKCE | Exempt — OAuth fix; token/refresh hardcoded CSRF-exempt |
| `POST /api/v1/auth/ws-token` | mandatory `get_current_user` | **Swept** (any authenticated role); CSRF-exempt via audited `/api/v1/auth/` prefix (login/refresh/ws-token are Bearer-or-JSON POSTs) |
| `/api/v1/auth` login/refresh/logout | unauthenticated / refresh-token | CSRF-exempt via audited prefix — outside the sweep (not user-principal mutating in the RBAC sense) |

**Companion assertion**: every exempted path either (a) carries no principal-resolving dependency, or (b) is an enumerated channel with a dedicated non-role auth mechanism. `ws-token` is NOT exempt (it IS swept — but CSRF-exempt via the audited prefix, which is fine: it requires a Bearer).

**Strict introspection test**: shared `get_all_apiroutes(app)` helper (extract from `test_api_contract.py:111-124`, parameterized, recurses nested `_IncludedRouter`). Assert: (1) **bidirectional map coverage** (every mutating-principal route has a map entry AND every map entry exists as a route — catches single-route deletion AND new unmapped routes; this is the load-bearing assertion); (2) variant-kind + permission-key + min-role per exact path. **Drop the TOTAL_FLOOR canary and the bare tag assertion as redundant** (iteration-7 — the bidirectional map subsumes both); **prefer version-tolerant route extraction** (public `isinstance` checks / OpenAPI-derived inventory) so FastAPI upgrades aren't hostage to this test; pin only if an upgrade actually breaks extraction.

### Denial audit + observability

`permission.denied` log + counter (principal, permission, required, actual); `permission.allowed`; `permission.live_role_read_failed`; `permission.api_key_role_cap`; `permission.kill_switch_read_failed`. REST 403 detail names permission + required role.

### Frontend

Inventory mutation flows per role; onboarding `seed-examples`/`starter-pipeline` acute case — creation-only exemption vs minimum-role gate decided in the matrix. Populate manifest `required_permissions` or document frontend out of scope; reconcile `required_roles` exact-match vs hierarchy. Permission-denied UX. Live role in `/me` + WS tokens. **OAuth: the SPA authorize route (302 target + approve POST) — minimal-correct v1, no dedicated consent page UI** (the authenticated approve IS the consent; deferred page documented).

### Kill switch — tenancy-bounded (DECISION 3; iterations 4-7)

`authz.enforce` resolved per-org from **`organisations.authz_enforce BOOLEAN NOT NULL DEFAULT TRUE` — a DEDICATED COLUMN, not `settings_json`/`jsonb_set`** (iteration-7: `settings_json` is `sa.JSON`; `jsonb_set` cannot target it without a cast, is net-new SQL with no precedent, and doesn't exist on MariaDB/SQLite — a dedicated boolean column is atomic at statement level and multi-backend safe). Migration in A1a. Setter route: named in the matrix (`org.admin`-gated write endpoint). **NOT `get_effective_setting`** (SystemConfig has no `organisation_id` — global fallback recreates the cross-tenant channel); `authz.enforce` never in SystemConfig (test/semgrep). **Set on BOTH surfaces**: (a) MCP — `McpAuthMiddleware` resolves per-request into a ContextVar; (b) **REST — `require_permission`/`get_current_tenant_user` resolves it per-request via the DI session** (no parent-app middleware exists — without a REST setter, `enforce=false` is inert on the swept surface). Per-request `token.reset()`; threadpool/Celery default to enforce (documented). On read error → default-enforce + counter. **Fresh SELECT, never the ORM identity-map attribute** (iteration-7: `session.get(Organisation,...).settings_json` can observe a stale pre-flip value; the read must be a dedicated SELECT on the membership-read statement). **Scope pinned (iteration-7)**: `enforce=false` lifts the org-role comparison in **ALL `require_permission` variants** (tenant, target-org-role reads/mutations, team-floor gate) — but **destructive mutations (org deletion `require_system_or_org_admin`, purge, deactivate) are NEVER lifted**, and the carve-outs (API-key cap, OAuth scope-cap, HITL guard) stay live. **Inline guards** (`_require_admin` at `backend/src/modulo/api/routes/api_keys.py:75`, org-management routes in `backend/src/modulo/api/routes/admin.py`) are NOT DI variants — the introspection map tags each swept route with its kill-switch behavior so coverage is explicit. `permission.denied` rate alert as flip trigger. **Tests**: `resolve_authz_enforce(session, org_id) -> bool` directly-callable (unit: org-branching fake; integration: real second org row); REST `enforce=false` lifts a 403 (explicit); org deletion NOT lifted; carve-outs intact; default-on; SystemConfig row no effect; boolean semantics; concurrent-write atomicity (column = free).

### Rollout — 3 PRs

1. **PR A1a**: migration 1 (owner drop, hard-fail after retry) + **`authz_enforce` column** + `_run_migrations` retry-FATAL (bounds inside) + live-read everywhere + REST + MCP kill-switch setters + 8-site cleanup + ORM + docs + BDD prefix/flip-table + registry + 5 REST variants + MCP delegation + `_VALID_ROLES` rewrite + 5 missing tools + `deliver_manual` + dead key + 3 alias removals (full consumer inventory) + MCP structural coverage + team RLS-parity floor + runs org-role-floor + denial observability + matrix deliverable. **Code-revertible** (owner-drop migration irreversible but code reverts safely — zero owner rows post-migration, live-read degrades).
2. **PR A1b**: OAuth consent (minimal-correct) + migration 2 (code DELETE + NOT NULL + `oauth_consent_states`) + existing OAuth test migration. **Fully revertible** (migration 2 has a trivial downgrade).
3. **PR A2 (independently revertible)**: API-key role-cap + live re-validation (`_clamp_role` helper; middleware seam scoped to the API-key branch) + counter/notify + tests. Land immediately after A1a; revert drill in runbook.
4. **PR B (single sweep PR — iteration-7: the per-resource split's security value didn't hold — the window stays open until the LAST per-resource PR regardless; the split only added allowlist-maintenance surface)**: convert endpoints file-by-file within the one PR (each with BDD/unit updates), scoped-hybrid routes, strict introspection test (bidirectional map + variant-kind/permission-key/min-role) committed last with `allowlist == ∅`, frontend reconciliation, AC1/AC9 verification. **Gate on A2 green.** Rollback = revert PR B.

**Rollback**: revert B (sweep) or A2 (caps) independently; A1b fully revertible; A1a code-revertible (migration irreversible). `infra_blocked` producer defined; consumer wiring is a follow-up task.

### Phase-2 RLS `WITH CHECK` — corrected (deferred, own review)

Hierarchy **levels**, not lexical strings; confirm `modulo_app` is not the table owner; non-Postgres: no backstop — surface on `/healthz`; pin non-Postgres builds out of PR CI. **Note (iteration-7)**: RLS is `USING`-only today, so INSERT on team-scoped tables is unrestricted at the DB layer — the app-gate is the only write-side team control on create paths. The introspection test must assert every mutating route on a team-scoped resource declares a team-aware check (owner_team_id source from the matrix) — a swept route that resolves the permission key but omits the team clause passes AC1 and lets a team-runner create rows owned by another team. Pulling the `WITH CHECK` work forward into PR B is recommended so the DB is the backstop.

### Testing (mandatory deliverable)

- Strict introspection test (PR B, committed last): bidirectional map + variant-kind + permission-key + min-role + team-clause assertion; shared `get_all_apiroutes(app)`; version-tolerant extraction.
- `assert_org_role` boundary matrix: each role × each required level × unknown/empty/None × bare `owner` claim → deny; `review_hitl:deliver_manual` row.
- MCP delegation: 4 action keys; structural coverage (registry walk, tool-set equality, mcp pinned).
- Migration tests: owner-drop (seed at pre-A1a → migrate → zero rows, constraint rejects, idempotent); OAuth migration 2 on its own container (raw-SQL seed at the intermediate revision since HEAD ORM requires the new columns; **patch `DATABASE_URL` for the test-local container** — alembic `env.py` resolves against it; runs two containers concurrently — acceptable, mark integration). AC5 = DB-level owner→admin + CHECK; stale-`owner`-claim denial in the boundary matrix.
- OAuth: full test list from the OAuth section (incl. CSRF-enabled e2e with negative control on a fresh wrapped app — NOT entering the lifespan; state double-consume/TTL; verifier forwarding).
- API-key (A2): `_clamp_role` pure tests; middleware dispatch-level demote test (full patch set); counter.
- Team floor: 5 cases + runs org-role-floor + service-layer gate-removal test.
- Kill-switch: `resolve_authz_enforce` unit + two-org integration; REST `enforce=false` lifts a 403; org deletion NOT lifted; carve-outs intact; default-on; SystemConfig row no effect; boolean semantics.
- BDD: real role clients, `/api/v1` prefixes, per-scenario flip table, runner-via-JWT, team-scope scenario deferred, mcp_oauth.feature rewritten (form-encoded, 302 authorize, tags removed), alias-consumer flips.
- Existing-test audit: `test_account_model.py`, 44 direct-role-principal unit tests, `test_mcp_security.py` (:44 + :55-70), `test_scope_validator.py:209`, alias consumers (complete inventory), OAuth tests (corrected attribution + ~70 count).

### Acceptance criteria

1. Strict introspection green (post-PR B): zero mutating user-principal routes without a permission dependency, none wrong variant/key/min-role, no team-clause omission; bidirectional; `allowlist == ∅`.
2. MCP structural coverage green: every mutating registered tool scoped; tool-name set equals pinned list.
3. Migration: zero `owner` rows; hard-fail active; OAuth code table emptied + NOT NULL + consent-states table.
4. Denial observability live: all 5 counters.
5. Migration test green (DB-level): formerly-owner → admin; bare `owner` claim denied.
6. OAuth: no token without consent session + PKCE; RFC 6749 form-urlencoded works; CSRF-enabled e2e with negative control green; demoted user's access clamped on next call, refresh denied.
7. API-key: viewer cannot mint; runner cannot mint operator; demoted key degrades (clamp, NOT persisted); removed owner's key dies.
8. Team floor green: org-admin/no-membership allowed (RLS parity); runs org-role-floor; no capability regression.
9. Full backend + frontend suites green (BDD role + OAuth + alias-flip scenarios real).

## Related Documents

- `hitl-gate-removal-guard-plan.md` (currently at `Repos/admin/strategy/competition/` — link or fold into ADR before implementation)
- PRD §9.2 / §9.3 (fix intro + §9.3.1), PRD §8.8, PRD §5.2, PRD §8.17 (PKCE S256), PRD alias-preservation sections (superseded)
- ADR 005 — Single org, teams as separation boundary
- Delivery tasks: `task-authz-permission-model`, `task-authz-centralized-enforcement`, `task-authz-owner-alias-fix` (phase-authz)

## Review History

- **Iteration 1**: 4 critical — JWT staleness; rollout order; 4 unguarded MCP tools; OAuth escalation.
- **Iteration 2**: 1 critical — system-permission fall-through = escalation; OAuth premise corrected; API-key parallel.
- **Iteration 3**: 0 critical — OAuth /refresh staleness; scoped-hybrid; kill-switch tenant control; window ineffective.
- **Iteration 4**: 0 critical — team shim contradiction; OAuth consent unimplementable; kill-switch mechanism; window deleted; 5th tool.
- **Iteration 5**: 2 critical — API-key clamp no-op (seam); `/mcp/oauth/token` CSRF-blocked. Fixed in v7, confirmed in iteration 6.
- **Iteration 6**: 0 critical, ~15 major — clamp must RETURN not mutate; kill-switch REST setter missing; 3 aliases; consent deny-path; A1 revertibility; state nonce; CSRF hardcode; e2e harness; verifier forwarding; two-orgs test; deliver_manual; cross-org; query budget; write race; boolean coercion; B split; infra_blocked.
- **Iteration 7**: 0 critical, ~10 major — CSRF exact-path switch would 403 webhook/login (keep audited prefixes); kill-switch scope vs variants/inline guards undefined; OAuth test placement vs A1a/A1b; B per-resource allowlist = false-negative hazard (collapse to single B); `jsonb_set` unimplementable on JSON column (dedicated boolean column); alias consumer inventory incomplete (Remy runtime + frontend + 6 tests + PRD ~12 refs); consent state store unpinned (oauth_consent_states table + TTL + cleanup); clamp seam ambiguous (pin ONE: `_clamp_role` pure helper + middleware); `infra_blocked` has no consumer (follow-up task); team-clause outside introspection (add assertion + recommend WITH CHECK forward); deny path auth; runs special case; reads-variant sys-admin bypass; introspection redundancy (drop canary/tag); `_run_migrations` double-run. **v9 produced — declared converged.**
