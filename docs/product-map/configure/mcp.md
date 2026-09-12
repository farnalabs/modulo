---
id: feat-mcp
prd: N/A
adr: [Repos/devtools/adr/017-centralized-authorization.md]
code:
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/api/mcp_tool_registry.py
  - backend/src/modulo/core/mcp/scope_validator.py
  - backend/src/modulo/api/routes/mcp_oauth.py
  - backend/src/modulo/api/routes/mcp_setup.py
  - frontend/src/views/SettingsMcpView.vue
unit-tests:
  - backend/tests/unit/mcp/test_scope_validator.py
  - backend/tests/unit/mcp/test_tenant_context.py
  - backend/tests/unit/mcp/test_api_key_mgmt_tools.py
  - backend/tests/unit/mcp/test_get_run_output.py
  - backend/tests/unit/mcp/test_mcp_connector_tools.py
  - backend/tests/unit/mcp/test_team_binding_enforcement.py
  - backend/tests/unit/test_mcp_security.py
  - backend/tests/unit/test_mcp_structural_coverage.py
  - frontend/src/__tests__/SettingsMcpView.spec.ts
bdd:
  - backend/tests/bdd/features/mcp/library_browse.feature
  - backend/tests/bdd/features/mcp/trigger.feature
  - backend/tests/bdd/features/mcp/mcp_oauth.feature
  - backend/tests/bdd/features/mcp/onboarding.feature
  - backend/tests/bdd/steps/test_alpha_mcp.py
  - backend/tests/bdd/steps/test_mcp_onboarding_steps.py
  - backend/tests/bdd/steps/test_mcp_oauth.py
depends-on: [feat-auth, feat-model-backends]
status: covered
---

# Model Context Protocol (MCP)

Remote MCP server through which external agents (Claude Code, IDE agents, Remy)
drive the Modulo ViewModel as a tool stack. Mounted at `/mcp` as a Starlette
sub-application, it exposes the pipeline/schema/connector/trigger/viewmodel tool
surfaces over MCP (SSE), authenticates by API key (`mk_*` bearer) or OAuth, and
enforces role / tenant / team scoping at both the middleware and viewmodel
layers. The `/settings/mcp` surface configures keys, their roles, and the MCP
URL, plus completion handoff setup. Built on the auth + model-backend core.

## Behaviours

- [x] The remote MCP server mounts at `/mcp` (FastMCP over Starlette, SSE
      streaming) and exposes the registered tool stack — run, pipeline, schema,
      library, connector, trigger, secret, runtime and viewmodel tools — as a
      thin adapter over the ViewModel API with per-tool definitions emitted via
      `mcp_tool_registry.build_tool_registry`
- [x] Authentication is API-key bearer (`Authorization: Bearer mk_<key>`):
      `McpAuthMiddleware` validates the key at the HTTP layer, rejects
      unauthenticated requests, and sets org_id/role in a ContextVar for tool
      handlers (operator vs runner roles); tenant org context is validated
      per-event on SSE streams
- [x] Dual-layer scope enforcement: the middleware gate is re-checked at the
      viewmodel layer by `core/mcp/scope_validator.py` against the centralized
      permission registry (ADR 017) — a bypass of the middleware cannot widen a
      tool's effective role — and team-bound tools enforce the caller's team
      binding
- [x] OAuth 2.0 client management (browser-authenticated): register/list/delete
      OAuth clients (`POST/GET/DELETE /api/v1/mcp/oauth/clients`) and approve
      pending browser consent (`POST /api/v1/mcp/oauth/consent/approve`), with
      the authorize/token/refresh protocol endpoints served by the MCP sub-app
- [x] Completion setup handoff: `POST /api/v1/mcp-setup` consumes a one-time
      setup token from an MCP tool response, configures the returned API key on
      the target model backend, and completes the setup flow
- [x] The `/settings/mcp` view lists the MCP URL, creates API keys with a
      selectable role (`settings-mcp-create-key`), revokes keys, and shows the
      generated key value for copying — the configured key is what external
      agents authenticate with
- [x] Security hardening: keys are minted/revoked through the api-key surface,
      list-run/cost and other sensitive tools are role-gated, HITL-gated tools
      route through human review, and the suite guards structural tool
      coverage so new tools cannot ship unscoped
- [x] Every API key carries an immutable caller scope (`org` | `user`,
      ADR 030/FAR-620): user-scoped keys act as their creator and are
      REST-JWT-minted only (flag + 10-key quota gated); MCP minting stays
      org-only. Mint/revoke emit `api_key_created` / `api_key_revoked` audit
      events on BOTH surfaces with `auth_type` / `key_scope` masked-prefix
      payload stamps
- [x] Caller-scoped (`.self` permission-key suffix) MCP tools target the
      CALLER's own account with NO target parameter (registry-introspection
      pinned); the first pair (`get_hitl_email_alerts` /
      `set_hitl_email_alerts`, `hitl_email.self` @ viewer, FAR-614) reads and
      writes the caller's own HITL email-alert preference and is
      denied-under-org-keys with the pinned `insufficient_scope` error shape
      (visible-but-failing in tools/list); all of its writes go through the
      single row-locked preferences writer
      (`db/crud/account.set_hitl_email_preference`)

## Known Gaps

- **OAuth protocol endpoints are `aiohttp`/session-bearing** — refresh/consent
  flows depend on the MCP sub-app lifetime; a separate process restart clears
  in-flight browser consent sessions.
- **SSE is the only transport exposed** — the streamable-HTTP transport is not
  published as a distinct surface here.

## QA History
- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/settings/mcp`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-09: **improve-architecture (product-map walk)** — closed the
  "no executing BDD surface for MCP onboarding" gap. `mcp/onboarding.feature`
  is now registered by `tests/bdd/steps/test_mcp_onboarding_steps.py` and
  executes against the shipped contracts: the discoverable tool inventory
  (FastMCP tool manager, the same registry `test_mcp_structural_coverage.py`
  pins) and the fail-closed auth gate (`McpAuthMiddleware` rejects
  unauthenticated / invalid-credential requests with 401, ADR 017). The draft
  previously described a PUBLIC `tools/list` that does not match the shipped
  server (its middleware rejects unauthenticated introspection fail-closed),
  so it was rewritten to describe the real contract and dropped from the
  tracked orphaned-BDD debt list.
- 2026-08-30: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-mcp` manifest feature (route `/settings/mcp`,
  previously absent from the feature graph). Behaviours verified against
  `api/mcp_server.py`, `api/mcp_tool_registry.py`, `core/mcp/scope_validator.py`,
  the OAuth + setup-handoff routes, the `tests/unit/mcp/*` scope/tenant/team
  suites, and the `mcp/` BDD features. Status: covered.
