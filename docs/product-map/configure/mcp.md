---
id: feat-mcp
prd: N/A
adr: [ADR 047 (centralized-authorization)]
code:
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/api/mcp_tool_registry.py
  - backend/src/modulo/api/db_error_handling.py
  - backend/src/modulo/api/routes/run_ws.py
  - backend/src/modulo/core/mcp/scope_validator.py
  - backend/src/modulo/api/routes/mcp_oauth.py
  - backend/src/modulo/api/routes/mcp_setup.py
  - backend/src/modulo/api/routes/oauth_metadata.py
  - frontend/src/views/SettingsMcpView.vue
  - frontend/src/components/settings/McpOauthClientsCard.vue
unit-tests:
  - backend/tests/unit/mcp/test_scope_validator.py
  - backend/tests/unit/mcp/test_tenant_context.py
  - backend/tests/unit/mcp/test_api_key_mgmt_tools.py
  - backend/tests/unit/mcp/test_get_run_output.py
  - backend/tests/unit/mcp/test_mcp_connector_tools.py
  - backend/tests/unit/mcp/test_team_binding_enforcement.py
  - backend/tests/unit/mcp/test_mcp_tool_exception_error.py
  - backend/tests/unit/mcp/test_mcp_session_contract_errors.py
  - backend/tests/unit/api/test_oauth_metadata.py
  - backend/tests/unit/api/test_run_ws.py
  - backend/tests/unit/test_mcp_security.py
  - backend/tests/unit/test_mcp_structural_coverage.py
  - frontend/src/__tests__/SettingsMcpView.spec.ts
bdd:
  - backend/tests/bdd/features/mcp/library_browse.feature
  - backend/tests/bdd/features/mcp/trigger.feature
  - backend/tests/bdd/features/mcp/review_hitl.feature
  - backend/tests/bdd/features/mcp/human_only.feature
  - backend/tests/bdd/features/mcp/mcp_oauth.feature
  - backend/tests/bdd/features/mcp/onboarding.feature
  - backend/tests/bdd/steps/test_alpha_mcp.py
  - backend/tests/bdd/steps/test_mcp_onboarding_steps.py
  - backend/tests/bdd/steps/test_mcp_oauth.py
depends-on: [feat-auth, feat-model-backends]
status: covered
---

# Model Context Protocol (MCP)

Remote MCP server through which external agents (Claude Code, IDE agents, Assistant)
drive the Modulo ViewModel as a tool stack. Mounted at `/mcp` as a Starlette
sub-application, it exposes the pipeline/schema/connector/trigger/viewmodel tool
surfaces over MCP (SSE), authenticates by API key (`mk_*` bearer) or OAuth, and
enforces role / tenant / team scoping at both the middleware and viewmodel
layers. The `/settings/mcp` surface configures keys, their roles, and the MCP
URL, plus completion handoff setup and the org's registered OAuth client
applications. Built on the auth + model-backend core.

## Behaviours

- [x] The remote MCP server mounts at `/mcp` (FastMCP over Starlette, SSE
      streaming) and exposes the registered tool stack – run, pipeline, schema,
      library, connector, trigger, secret, runtime and viewmodel tools – as a
      thin adapter over the ViewModel API with per-tool definitions emitted via
      `mcp_tool_registry.build_tool_registry`
- [x] Authentication is API-key bearer (`Authorization: Bearer mk_<key>`):
      `McpAuthMiddleware` validates the key at the HTTP layer, rejects
      unauthenticated requests, and sets org_id/role in a ContextVar for tool
      handlers (operator vs runner roles); tenant org context is validated
      per-event on SSE streams
- [x] Dual-layer scope enforcement: the middleware gate is re-checked at the
      viewmodel layer by `core/mcp/scope_validator.py` against the centralized
      permission registry (ADR 047) – a bypass of the middleware cannot widen a
      tool's effective role – and team-bound tools enforce the caller's team
      binding
- [x] OAuth 2.0 client management (browser-authenticated): register/list/delete
      OAuth clients (`POST/GET/DELETE /api/v1/mcp/oauth/clients`) and approve
      pending browser consent (`POST /api/v1/mcp/oauth/consent/approve`), with
      the authorize/token/refresh protocol endpoints served by the MCP sub-app
- [x] Completion setup handoff: `POST /api/v1/model-backends/{backend_id}/complete-setup`
      (`api/routes/mcp_setup.py`, `tests/unit/api/test_mcp_setup.py`) consumes a
      one-time setup token from an MCP tool response, configures the returned API
      key on the target model backend, and completes the setup flow
- [x] The `/settings/mcp` view lists the MCP URL, creates API keys with a
      selectable role (`settings-mcp-create-key`), revokes keys, and shows the
      generated key value for copying – the configured key is what external
      agents authenticate with
- [x] The MCP API-keys card distinguishes a **revoked** key from an
      **expired** one by observed facts, not the derived `is_active` flag
      (FAR-1296 / FAR-1299): the key serializer
      (`auth/api_key._serialize_key`) emits `revoked_at` – ISO-8601 when set,
      else `null`, "the revocation FACT, not an inference from `is_active`"
      – alongside `lookup_prefix` / `role` / `scope` / `last_used_at` /
      `expires_at`, and revocation is a `DELETE /api/v1/api-keys/{key_id}`
      (`api_keys.py` route; the earlier PUT that silently dropped was fixed
      by FAR-1291). The `/settings/mcp` table renders a per-key status badge
      (`settings-mcp-key-status`) computed from the two facts: `revoked`
      (`revoked_at` set – an operator's action, checked first), `expired`
      (`revoked_at` null and the expiry elapsed – nobody revoked it), or
      `active` – with the revoke button shown only for `is_active` keys
      (`frontend/src/views/SettingsMcpView.vue`,
      `frontend/src/__tests__/SettingsMcpView.spec.ts`,
      `backend/tests/unit/api/test_api_keys_endpoint.py`)
- [x] MCP OAuth client registration UI (2026-09-28, FAR-1251): the
      "Registered OAuth Clients" card on `/settings/mcp`
      (`components/settings/McpOauthClientsCard.vue`) lists the org's clients
      – name, client id, scopes, redirect URIs, created – and drives
      registration and revocation through `POST/GET/DELETE
      /api/v1/mcp/oauth/clients`. Registering takes a required name, one or
      more redirect URIs (tokenised on ANY whitespace so the backend's
      space-joined storage round-trips losslessly, every entry an absolute
      `http://`/`https://` URI – localhost and 127.0.0.1 stay valid for local
      development, duplicates de-duplicated, invalid entries named in an
      inline error) and at least one of the three valid scopes `trigger:run` /
      `hitl:review` / `library:browse` (the backend's `VALID_SCOPES`; an
      unknown scope is a 400 `InvalidScopeError`). Success opens a one-time
      dialog showing the client id and client secret: only the create
      response ever carries the raw secret (the row stores
      `client_secret_hash`, so it cannot be retrieved again), the value
      auto-masks after 10 s, both values are wiped from component state when
      the dialog closes, and a rejected clipboard write (plain-HTTP
      self-hosted origin, no `navigator.clipboard`) renders a visible
      copy-failed message rather than losing the credential silently. Revoke
      confirms by name, then `DELETE`s the client by id (404 when already
      gone). All three endpoints require org role `admin` or `operator` – a
      viewer gets 403 on list/register/delete alike and the card renders a
      restricted state instead of the table (the list call's 403 is the
      fallback for a stale/downgraded JWT) – and registration refuses with
      500 while `MODULO_PUBLIC_URL` is unset or still the
      `http://localhost:8000` default, because the authorize / token / refresh
      protocol endpoints need a real public issuer
      (`SettingsMcpView.spec.ts`, `tests/unit/api/test_mcp_oauth.py`,
      `mcp/mcp_oauth.feature`)
- [x] Security hardening: keys are minted/revoked through the api-key surface,
      list-run/cost and other sensitive tools are role-gated, HITL-gated tools
      route through human review, and the suite guards structural tool
      coverage so new tools cannot ship unscoped
- [x] MCP trigger dispatch (2026-09-22): the `trigger_pipeline` tool drives the
      real manual-run path (snapshot + `create_run` with the caller's account,
      `trigger_type manual`, then dispatch) and is scope-gated through the
      centralized permission registry (`run.trigger` at runner), with the 401
      auth gate enforced by `McpAuthMiddleware`; the five
      `mcp/trigger.feature` scenarios execute in CI against these real handler
      and middleware seams (the tool handlers are invoked directly with the
      request ContextVars hydrated by hand, so the FastMCP invoke/dispatch layer
      itself is not exercised)
- [x] MCP HITL review (2026-09-24): the `review_hitl` unified gate tool (claim /
      approve / reject / deliver_manual) drives the REAL parse guard, scope gate
      and decision dispatch – `approve`/`reject` require a claim token
      (`claim_token_required` otherwise), the `_check_agent_tool_scope`
      role-hierarchy chokepoint denies a `runner` `hitl:review` actions with the
      pinned `insufficient_scope` error shape, and a successful decision reports
      `{"status": "approved"|"rejected", "review_id": ...}` through the real
      HITLManager; `list_pending_hitl` returns the org's undecided gates with the
      shared description resolver. Five `mcp/review_hitl.feature` scenarios
      execute in CI against these real handler seams (auth re-validation + DB /
      HITLManager seams patched), closing the last legacy `/mcp/tools/call`
      `@awaiting-implementation` draft under the review surface
- [x] MCP human-only gate enforcement (2026-09-24): the REAL `review_hitl`
      policy hook `_check_human_only_gate` denies an API-key/MCP principal every
      decision action (claim/approve/deliver_manual) on a `human_only` gate –
      the shared `human_only_denial` verdict returns the pinned
      `{"error": "human_only_gate", "detail": ...}` error shape (fail-closed
      even when the config is unresolvable but the gate fired) and every denial
      appends the FAR-634 `hitl.human_only_denied` audit event; `reject` is not
      an escape hatch because `claim` is itself denied, so an agent can neither
      claim nor decide such a gate. `list_pending_hitl` additionally surfaces a
      per-gate `human_only` flag via the shared batched flag resolver
      (`db/crud/hitl_review_config.resolve_gate_human_only_map` – claim-stamped
      fire-time config preferred, snapshot-config fallback, fail-safe
      `DEFAULT_HUMAN_ONLY` default) so an MCP client can SEE which pending gates
      require a browser human before attempting an action. The three
      `mcp/human_only.feature` scenarios execute in CI against these real
      handler + policy seams – the denial path (`review_hitl` approve on a
      seeded `{"human_only": true}` gate), the list path (gate listed with
      `human_only: true`), and the FAR-611 decision-audit attribution
      (`client_type` `browser` for a REST browser principal via
      `hitl._client_type` vs `mcp` stamped by the MCP `_dispatch_hitl_action`)
      – with only the auth re-validation and DB / config-resolution /
      HITLManager seams patched
- [x] Every API key carries an immutable caller scope (`org` | `user`,
      ADR 030/FAR-620): user-scoped keys act as their creator and are
      REST-JWT-minted only (flag + 10-key quota gated); MCP minting stays
      org-only. Mint/revoke emit `api_key_created` / `api_key_revoked` audit
      events on BOTH surfaces with `auth_type` / `key_scope` masked-prefix
      payload stamps
- [x] MCP library browse (2026-09-25): the `search_library` tool is the
      read-only library-browse surface (browse + text search + cursor
      pagination over org / Native / community primitives). It stays on the
      read-only allowlist (pinned at the `resource.read_only` viewer floor) in
      the centralized scope
      gate (`core/mcp/scope_validator.py`) and – following the
      `copy_library_primitive` (library.copy @ runner) precedent – is now
      gated at the handler by the real `_check_agent_tool_scope` chokepoint:
      an authenticated caller at or above viewer browses, and a node-level
      `capability_scope.allowed_tools` that excludes `search_library` (a
      run-scoped sandbox key narrowed to `trigger`-only tools) is denied the
      pinned `{"error": "insufficient_scope", "detail": ...}` shape BEFORE
      any DB read. The four `mcp/library_browse.feature` scenarios execute
      in CI against the real handler + scope-gate seams (auth re-validation +
      `list_primitives` / `_session` seams patched), closing the last pinned
      legacy-`/mcp/tools/call` draft
- [x] Caller-scoped (`.self` permission-key suffix) MCP tools target the
      CALLER's own account with NO target parameter (registry-introspection
      pinned); the first pair (`get_hitl_email_alerts` /
      `set_hitl_email_alerts`, `hitl_email.self` @ viewer, FAR-614) reads and
      writes the caller's own HITL email-alert preference and is
      denied-under-org-keys with the pinned `insufficient_scope` error shape
      (visible-but-failing in tools/list); all of its writes go through the
      single row-locked preferences writer
      (`db/crud/account.set_hitl_email_preference`)
- [x] MCP tool errors carry specific, branchable codes (FAR-1502): `_tool_error`
      REQUIRES a keyword-only `code` (no default), so no call site can fall back
      to the disallowed generic `internal_error`; the shared `_tool_exception_error`
      classifier maps every escaped exception type to a code the consuming agent
      can branch on — `insufficient_scope` (`MCPAuthorizationError`),
      `validation_failed` (4xx `StarletteHTTPException`) / `server_error` (5xx),
      `session_contract_error` (`InvalidRequestError` / `MissingGreenlet`) via the
      shared `db_error_handling` classifier, `conflict` (`IntegrityError`),
      `migration_required` (`ProgrammingError`), `database_unavailable` (any other
      `SQLAlchemyError`, incl. a transient `OperationalError` /
      `PendingRollbackError`), and `server_error` as the ONLY reserved catch-all
      (`backend/src/modulo/api/mcp_server.py` `_tool_error` /
      `_tool_exception_error`, `backend/src/modulo/api/db_error_handling.py`
      `session_contract_error_payload`; `test_mcp_tool_exception_error.py`,
      `test_mcp_session_contract_errors.py`)
- [x] The run WebSocket control-frame vocabulary aligns to the MCP codes
      (FAR-1502): the frames answer `database_unavailable` (the old
      `db_unavailable` key no other surface used) and `server_error` (the old
      generic `internal_error`), and the `SQLAlchemyError` arm runs through the
      SAME shared session-contract classifier so a programming bug gets the
      specific `session_contract_error` frame instead of a retry-inviting outage
      frame — no consumer of the old keys exists outside the backend
      (`backend/src/modulo/api/routes/run_ws.py`; `test_run_ws.py`);
      `backend/tests/architecture/test_mcp_error_codes.py` structurally forbids
      the `internal_error` payload in both sources and keeps `_tool_error`'s
      `code` keyword-only with no default
- [x] OAuth discovery metadata (2026-10-07, FAR-1476): two unauthenticated
      `/.well-known/` documents let a stock MCP harness bootstrap OAuth from a
      bare Modulo URL – the RFC 8414 authorization-server metadata
      (`/.well-known/oauth-authorization-server`: issuer, the
      `/mcp/oauth/authorize` + `/mcp/oauth/token` endpoints, `code` response
      type, `authorization_code` / `refresh_token` grants, PKCE `S256`,
      client-secret auth methods) and the RFC 9728 protected-resource metadata
      (`/.well-known/oauth-protected-resource` plus its `/mcp` path-suffix
      variant, which MCP clients ask for too: resource `/mcp/`,
      `authorization_servers`, header bearer methods) – with `scopes_supported`
      derived from the single source of truth `auth.oauth.VALID_SCOPES`
      (`trigger:run` / `hitl:review` / `library:browse`) in both documents.
      They are pre-auth and org-less by design: a harness must read them before
      it holds any credential, so they cannot consult the per-org `mcp_server`
      kill switch, which stays enforced where it always was – at `/mcp`
      (`McpAuthMiddleware` -> `_call_next_if_feature_enabled`) and inside the
      `/mcp/oauth/{authorize,token,refresh}` protocol endpoints; discovery
      describes the server, it grants nothing. The routes are registered ahead
      of the SPA fallback mount, so `/.well-known/*` can no longer answer
      `index.html`, and stay out of `openapi.json` (`include_in_schema=False`).
      Every unauthenticated MCP `401` now carries
      `WWW-Authenticate: Bearer resource_metadata="<public URL>/.well-known/oauth-protected-resource"`
      while `403`/policy denials and `5xx` do not (an authenticated principal
      told "not allowed" must not be invited to re-authenticate). No
      `registration_endpoint` is advertised: there is no RFC 7591 dynamic
      client registration, so a client is configured with an explicit
      client id rather than sent down a 404 path
      (`backend/src/modulo/api/routes/oauth_metadata.py`,
      `backend/src/modulo/api/mcp_server.py`, `backend/src/modulo/api/main.py`;
      `backend/tests/unit/api/test_oauth_metadata.py`)

## Known Gaps

- **OAuth protocol endpoints are `aiohttp`/session-bearing** – refresh/consent
  flows depend on the MCP sub-app lifetime; a separate process restart clears
  in-flight browser consent sessions.
- **SSE is the only transport exposed** – the streamable-HTTP transport is not
  published as a distinct surface here.

## QA History
- 2026-10-07: **Spec-accuracy pass** – FAR-1476 shipped the OAuth discovery
  surface (RFC 8414 authorization-server + RFC 9728 protected-resource
  documents, the `WWW-Authenticate` challenge on unauthenticated MCP `401`s, the
  `/.well-known/` proxy rules in the five deploy configs) but no product-map
  layer described it. Added the `code:` / `unit-tests:` citations
  (`api/routes/oauth_metadata.py`, `tests/unit/api/test_oauth_metadata.py`) and
  the checked behaviour line above. Known Gaps reviewed and unchanged: the
  session-bearing protocol-endpoint gap and the SSE-only transport gap both
  still stand. The `frontend/src/manifest.yaml` `feat-mcp` registry is untouched
  – its `behaviours:` list is a curated subset (the base mount/auth/scope and
  trigger-dispatch behaviours are tracker-only), and neither the feature
  description nor any route `product_map` ref changes for a pre-auth endpoint
  with no UI surface.
- 2026-10-06: **Improve Architecture product-map walk** — closed the untracked
  FAR-1502 surface: the MCP tool-error vocabulary that the consuming agent
  branches on shipped (specific, branchable codes via the required-keyword
  `_tool_error` + the shared `_tool_exception_error` classifier) and the run
  WebSocket control frames were aligned to the same codes, but neither
  product-map layer described it. Added the two checked behaviour lines and the
  `code:` / `unit-tests:` citations (`mcp_server.py`, `db_error_handling.py`,
  `run_ws.py`; `test_mcp_tool_exception_error.py`,
  `test_mcp_session_contract_errors.py`, `test_run_ws.py`) plus the
  `frontend/src/manifest.yaml` `feat-mcp` registry line.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-30: **Improve Architecture product-map walk** – closed the
  `feat-mcp` API-key sub-surface gap for FAR-1291 / FAR-1296 / FAR-1299: the
  key card's revoked-vs-expired status distinction (why `is_active: false`
  cannot say WHY a key stopped working) shipped 2026-09-29/30 (DELETE
  revocation, `revoked_at` in the serializer, the `settings-mcp-key-status`
  badge read in the Settings MCP view) but the behaviour surface was not
  described in either product-map layer. Added the behaviour line to the
  manifest `feat-mcp` registry and sharpened this tracker's API-key bullet,
  citing `auth/api_key._serialize_key`, `api_keys.py`, `SettingsMcpView.vue`
  and `SettingsMcpView.spec.ts` / `test_api_keys_endpoint.py`.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-25: **product-map review pass** – closed the last pinned MCP
  legacy-`/mcp/tools/call` draft: the four `mcp/library_browse.feature`
  scenarios previously targeted the dead HTTP surface and never ran. They are
  rewritten (the `trigger.feature` / `review_hitl.feature` re-anchor pattern)
  to drive the REAL `search_library` tool handler directly (request ContextVars
  hydrated by hand): the list surface (`id`/`name`/`type` wire items), the text
  search passthrough (the `search` term reaches the real `list_primitives`
  seam), the read-only posture (the tool is on the READ_ONLY_TOOLS allowlist
  and the only library seam it can touch is the read – `copy_library_primitive`
  is never invoked), and the scope-gate denial (a node-level
  `capability_scope.allowed_tools` that excludes `search_library` is denied the
  pinned `insufficient_scope` error by the real `_check_agent_tool_scope`
  chokepoint before any DB read). Product change closing the wire gap the
  scenarios describe: `search_library` stays on the read-only allowlist
  (`resource.read_only`, viewer) in `core/mcp/scope_validator.py` and now
  calls the shared
  `_check_agent_tool_scope` handler gate (mirroring `copy_library_primitive`),
  so the library-browse surface is explicitly scoped and the FAR-436 node-level
  allowed_tools narrowing can restrict it. Removed the four scenarios from
  `PINNED_AWAITING_IMPLEMENTATION` (`test_test_suite_safety_nets.py`);
  `_ORPHANED_BDD_FEATURES` stays empty. No `@awaiting-implementation`
  scenarios remain under `feat-mcp`.
- 2026-09-24: **product-map review pass** – closed the MCP HITL-review
  `@awaiting-implementation` gap: the five `mcp/review_hitl.feature` scenarios
  previously targeted the dead legacy `/mcp/tools/call` HTTP surface (pinned
  since 2026-08) and never ran. They are rewritten to drive the REAL
  `review_hitl` / `list_pending_hitl` tool handler functions directly (request
  ContextVars hydrated by hand), exercising the real `_parse_hitl_action`
  claim-token guard (`claim_token_required`), the real `_check_agent_tool_scope`
  role-hierarchy scope gate (a `runner` is denied `hitl:review` →
  `insufficient_scope`), the real `_check_human_only_gate` policy hook and the
  real HITLManager approve/reject decision dispatch (`approved` / `rejected` +
  `review_id`), plus the real pending-gate serialisation with the shared gate
  description resolver – network-free and DB-free with only the auth
  re-validation and DB/HITLManager seams patched. Removed the five scenarios
  from `PINNED_AWAITING_IMPLEMENTATION` (`test_test_suite_safety_nets.py`);
  `_ORPHANED_BDD_FEATURES` stays empty. The `library_browse` / `human_only`
  legacy-`/mcp/tools/call` drafts remain pinned as acknowledged gaps.
- 2026-09-24: **product-map review pass** – closed the `human_only` half of the
  remaining MCP gap: the three `mcp/human_only.feature` scenarios previously
  targeted the dead legacy `/mcp/tools/call` HTTP surface (pinned since
  2026-08) and never ran. Following the `trigger.feature` / `review_hitl.feature`
  re-anchor pattern, they now drive the REAL `review_hitl` / `list_pending_hitl`
  handler seams directly (request ContextVars hydrated by hand): the REAL
  `_check_human_only_gate` policy hook produces the pinned
  `{"error": "human_only_gate", "detail": MSG_HUMAN_ONLY_DENY}` verdict with the
  FAR-634 `hitl.human_only_denied` denial audit attempted; `list_pending_hitl`
  lists the human-only gate with its real per-gate `human_only` flag; and the
  FAR-611 decision-audit `client_type` attribution is exercised on both sides
  (`browser` via the REST `hitl._client_type` for a browser principal, `mcp`
  stamped by the MCP `_dispatch_hitl_action`). Added the per-gate `human_only`
  flag to the `list_pending_hitl` wire via the new shared batched resolver
  `db/crud/hitl_review_config.resolve_gate_human_only_map` (claim-stamped config
  preferred, snapshot fallback, fail-safe `DEFAULT_HUMAN_ONLY`), so an MCP
  client can SEE which pending gates require a browser human before acting.
  Removed the three scenarios from `PINNED_AWAITING_IMPLEMENTATION`;
  `_ORPHANED_BDD_FEATURES` stays empty. `library_browse` remains pinned as an
  acknowledged gap.
- 2026-09-22: **product-map review pass** – closed the "no executing BDD for
  the trigger tool" gap: the five `mcp/trigger.feature` scenarios previously
  targeted the dead legacy `/mcp/tools/call` HTTP surface (pinned
  `@awaiting-implementation`) and never ran. They are rewritten to drive the
  REAL shipped contract by calling the `trigger_pipeline` / `review_hitl`
  handler functions directly (request ContextVars hydrated by hand) – exercising
  the real `_check_agent_tool_scope` scope-gate chokepoint (manual run with
  `trigger_type manual` and the caller's account, `input_payload` passthrough,
  unknown-pipeline `pipeline_not_found` refusal), the real `McpAuthMiddleware`
  401 gate for unauthenticated requests, and the real role-hierarchy scope
  denial (a `runner` key triggers but cannot `review_hitl` `approve` →
  `insufficient_scope`) – network-free and DB-free with only the auth
  re-validation and DB/dispatch seams patched. The FastMCP invoke/dispatch layer
  itself is not exercised by these steps. Removed the five scenarios
  from `PINNED_AWAITING_IMPLEMENTATION` (`test_test_suite_safety_nets.py`);
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-12: **product-map review pass** – registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/settings/mcp`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-09: **product-map review pass** – closed the
  "no executing BDD surface for MCP onboarding" gap. `mcp/onboarding.feature`
  is now registered by `tests/bdd/steps/test_mcp_onboarding_steps.py` and
  executes against the shipped contracts: the discoverable tool inventory
  (FastMCP tool manager, the same registry `test_mcp_structural_coverage.py`
  pins) and the fail-closed auth gate (`McpAuthMiddleware` rejects
  unauthenticated / invalid-credential requests with 401, ADR 047). The draft
  previously described a PUBLIC `tools/list` that does not match the shipped
  server (its middleware rejects unauthenticated introspection fail-closed),
  so it was rewritten to describe the real contract and dropped from the
  tracked orphaned-BDD debt list.
- 2026-08-30: **product-map review pass** – new behaviour
  tracker for the registered `feat-mcp` manifest feature (route `/settings/mcp`,
  previously absent from the feature graph). Behaviours verified against
  `api/mcp_server.py`, `api/mcp_tool_registry.py`, `core/mcp/scope_validator.py`,
  the OAuth + setup-handoff routes, the `tests/unit/mcp/*` scope/tenant/team
  suites, and the `mcp/` BDD features. Status: covered.
