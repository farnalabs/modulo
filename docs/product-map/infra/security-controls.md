---
id: feat-infra-security
prd: 7,7.1,7.2,7.3,7.4,7.5,7.6,7.7,7.8,7.9,7.11,7.12,7.14,7.15,7.16,7.17
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/security/rls_enforcement.feature
  - backend/tests/bdd/features/security/dom_sensitive_data.feature
  - backend/tests/bdd/features/security/credential_store.feature
  - backend/tests/bdd/features/security/input_sanitization.feature
  - backend/tests/bdd/features/triggers/webhook_hmac.feature
  - backend/tests/bdd/features/auth/jwt_security.feature
  - backend/tests/bdd/features/auth/rbac.feature
  - backend/tests/bdd/features/auth/tenant_isolation.feature
  - backend/tests/bdd/features/auth/api_keys.feature
  - backend/tests/bdd/features/hitl/claim.feature
  - backend/tests/bdd/features/hitl/approve.feature
code:
  - backend/src/modulo/auth/jwt.py
  - backend/src/modulo/auth/dependencies.py
  - backend/src/modulo/auth/api_key.py
  - backend/src/modulo/auth/team_rbac.py
  - backend/src/modulo/auth/ws_token.py
  - backend/src/modulo/auth/passwords.py
  - backend/src/modulo/db/rls.py
  - backend/src/modulo/core/trigger_engine/__init__.py
  - backend/src/modulo/core/notifier/__init__.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/secrets_backend/fernet.py
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/events/event_bus.py
  - backend/src/modulo/connectors/base.py
  - backend/src/modulo/api/middleware/sensitive_mask.py
  - .semgrep/sandboxed_jinja2.yml
  - .semgrep/yaml_safe_load.yml
  - .semgrep/credential_in_state.yml
  - .semgrep/rls_set_local.yml
  - .semgrep/async_db_driver.yml
  - .semgrep/sensitive-data-in-log.yml
  - .semgrep/asyncio_waitfor_wait_unshielded.yml
  - .semgrep/non_serializable_in_langgraph_state.yml
unit-tests:
  - backend/tests/unit/auth/test_jwt.py
  - backend/tests/unit/auth/test_api_key.py
  - backend/tests/unit/auth/test_ws_token.py
  - backend/tests/unit/auth/test_passwords.py
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/notifier/test_notifier.py
  - backend/tests/unit/events/test_redis_broker.py
  - backend/tests/unit/api/test_auth_rate_limiter.py
depends-on:
  - feat-auth-jwt-auth
  - feat-auth-team-rbac
  - feat-teams-team-isolation
  - feat-core-secrets-backend
  - feat-pipelines-hitl-gates
  - feat-core-trigger-system
  - feat-auth-rate-limiting
status: partial
---

# Security Controls

Cross-cutting security controls documented across PRD §7. Includes sandboxed Jinja2 rendering, prompt injection guards, workflow bundle import safety, Ed25519 signing for community registry, connector access control, HITL atomic lock, inbound webhook HMAC, outbound webhook signing, TLS enforcement, LangGraph checkpoint data protection, team visibility server-side enforcement, MCP dual-layer scope enforcement, SSE per-event org context validation, and DOM sensitive data masking. These are enforced by semgrep rules, runtime guards, and pre-commit hooks — not by individual route handlers.

Individual subsections (7.10 JWT, 7.13 Secrets, 7.18 Rate Limiting) have dedicated product map entries with code/test references. This entry covers the cross-cutting security posture as a whole.

## Known Gaps

- **§7.4 Community Registry Ed25519 Signing**: Not yet implemented. The community registry feature is not live. The signing infrastructure (key pair generation, pinned trust anchor, client-side verification, key rotation mechanism) needs to be built alongside the community library.
- **§7.12 LangGraph Checkpoint Data**: Stored in plaintext (documented PRD known gap). V2 Fernet encryption of checkpoint blobs before storage is planned. Self-hosted admins with direct Postgres access to the `langgraph.*` schema can read all checkpoint blobs — this is an inherent property of self-hosted deployments. Mitigation: restrict Postgres access to the application service account only.
- **§7.2 Prompt Injection — content filtering (V1) missing**: Fully preventing injection is not possible. Verified implemented mitigations: SandboxedEnvironment prevents template-author RCE; per-agent input length limits (`max_input_length` → `truncate_input` in `pipeline_engine/input_truncation.py`, enforced in `node_runner.py`); output validation before connector writes is recommended practice. Missing: configurable content filtering per agent (V1). LLM-judge eval injection is a documented risk.
- [x] **RESOLVED (2026-08-15) — §7.14 Team Visibility — ViewModel layer enforcement**: Verified implemented. `viewmodel.py` `/api/v1/viewmodel/current` rejects `view_as_team` for any non-admin with 403 at the ViewModel command layer, and team-private pipelines are filtered server-side. BDD `teams/view_as_team_non_admin_rejected.feature` (7 scenarios) + unit tests cover operator/runner/viewer/API-key rejection and admin-demotion semantics.
- [x] **RESOLVED (2026-08-15) — §7.15 MCP Scope Enforcement — ViewModel command layer**: Verified implemented. Every MCP tool handler calls `check_tool_scope()` (ViewModel command re-validation) in addition to the token-middleware layer. `test_scope_validator.py`, `test_mcp_security.py`, and `test_mcp_structural_coverage.py` cover the dual-layer pattern.

## QA History

- 2026-08-15: Coverage-completion sweep. Verified and marked [x] 7 previously-unchecked behaviours: §7.5 Connector Access Control (ConnectorInstance owner/visibility/`allowed_operations` + `ConnectorACL` + graph-validator checks, tested in `test_acl.py`/`test_graph_validator.py`), §7.8 Outbound Webhook Signing (notifier `X-Modulo-Signature` + BDD `signing.feature`), §7.9 TLS (reference `deploy/caddy/Caddyfile` + `docs/deployment-security.md`), §7.11 GitHub scopes (`REQUIRED_FINE_GRAINED_PERMISSIONS` + `X-OAuth-Scopes` health check + `missing_scope:` codes, tested in `test_github_scopes.py`), §7.14 Team Visibility (ViewModel-layer `view_as_team` 403), §7.15 MCP dual-layer scope enforcement (`check_tool_scope` per tool), §7.16 SSE per-event org validation (`_revalidate_live_role`, `test_mcp_sse.py`). Confirmed §7.2 input-length limits + output validation are implemented (only V1 content filtering remains) and §7.4/§7.12 remain genuine gaps. Status: partial (12/15 behaviours covered).

## Behaviours

- [x] §7.1 Template Rendering — Sandboxed Jinja2 (`jinja2.sandbox.SandboxedEnvironment`) — enforced by semgrep rule `must-use-sandboxed-jinja2`
- [ ] §7.2 Prompt Injection — Configurable content filtering (V1) missing; input length limits (`max_input_length` truncation in `pipeline_engine/input_truncation.py` + `node_runner.py`) and output-validation-as-recommended-practice verified implemented
- [x] §7.3 Workflow Bundle Import — Safe YAML parsing (`yaml.safe_load()`) — enforced by semgrep rule `must-use-yaml-safe-load`
- [ ] §7.4 Community Registry — Ed25519 signing with pinned trust anchor (not yet implemented; community library is not live)
- [x] §7.5 Connector Access Control — `account_id` (owner), `owner_team_id`, `visibility` (`org`/`team`, DB CHECK `ck_connector_instances_visibility`), `allowed_operations` on `ConnectorInstance`; `ConnectorACL` enforces operation allowlist; `graph_validator` rejects over-privileged connector use at validation. Tests: `backend/tests/unit/connectors/test_acl.py`, `backend/tests/unit/graph_validator/test_graph_validator.py`
- [x] §7.6 HITL Claim — Atomic lock via `UPDATE ... WHERE account_id IS NULL AND decision IS NULL RETURNING`
- [x] §7.7 Inbound Webhook Security — HMAC-SHA256 with timestamp replay window (`_verify_hmac` in trigger_engine)
- [x] §7.8 Outbound Webhook Signing — `notifier._sign_payload` builds `sha256=<hmac>` over the body with the per-endpoint secret, sent as `X-Modulo-Signature`; BDD `backend/tests/bdd/features/notifications/signing.feature` (4 scenarios, wired via `test_alpha_notifications.py`)
- [x] §7.9 TLS — Reference Caddy configuration ships at `deploy/caddy/Caddyfile`; `docs/deployment-security.md` §1.1 and `docs/deployment.md` "TLS / HTTPS" state TLS termination is required before network exposure
- [x] §7.11 GitHub Connector OAuth Scopes — `REQUIRED_FINE_GRAINED_PERMISSIONS = {contents:read, contents:write, pull_requests:write}` declared in capability manifest; health check verifies `X-OAuth-Scopes` and reports `missing_scope:<perm>` codes. Tests: `backend/tests/unit/connectors/test_github_scopes.py`, `test_github_errors.py::test_health_check_missing_scope_codes`, BDD `connectors/github_connector.feature`
- [ ] §7.12 LangGraph Checkpoint Data — Plaintext known gap (documented in PRD), V2 Fernet encryption planned
- [x] §7.14 Team Visibility — `view_as_team` enforced at the ViewModel command layer: `viewmodel.py` `/api/v1/viewmodel/current` returns 403 for any non-admin carrying the parameter. BDD `backend/tests/bdd/features/teams/view_as_team_non_admin_rejected.feature` (7 scenarios) + `backend/tests/unit/api/test_viewmodel_endpoint.py`
- [x] §7.15 MCP Scope Enforcement — Dual-layer: `McpAuthMiddleware` token layer + per-tool `check_tool_scope()` ViewModel command re-validation on every MCP tool handler (create_pipeline, trigger_pipeline, review_hitl, …). Tests: `backend/tests/unit/mcp/test_scope_validator.py`, `test_mcp_security.py`, `test_mcp_structural_coverage.py`
- [x] §7.16 SSE Per-Event Org Context Validation — `mcp_server.py` re-validates the auth credential + live role per event for streaming SSE connections (`_revalidate_live_role`, ADR 017); tests in `backend/tests/unit/api/test_mcp_sse.py`
- [x] §7.17 DOM Sensitive Data Rule — Masked default, server-authenticated 30s reveal (sensitive_mask.py + reveal endpoint + BDD tests)
