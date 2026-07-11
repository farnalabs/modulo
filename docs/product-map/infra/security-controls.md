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
- **§7.2 Prompt Injection**: Fully preventing injection is not possible. Current mitigations: SandboxedEnvironment prevents template-author RCE, input length limits (`max_input_length`) are configurable per agent, output validation recommended as practice. Missing: configurable content filtering per agent (V1). LLM-judge eval injection is a documented risk.
- **§7.14 Team Visibility — ViewModel layer enforcement**: RBAC middleware at the token layer is implemented, but ViewModel command layer re-validation (§7.15 dual-layer pattern) is not yet applied to team visibility. Current enforcement relies on token-scoped RBAC and route-level checks.
- **§7.15 MCP Scope Enforcement — ViewModel command layer**: Token middleware layer enforcement exists (bearer token validation in MCP server). The second layer (ViewModel command re-validation) is not yet implemented for all MCP tools. Some tools rely on middleware-only scoping.

## Behaviours

- [x] §7.1 Template Rendering — Sandboxed Jinja2 (`jinja2.sandbox.SandboxedEnvironment`) — enforced by semgrep rule `must-use-sandboxed-jinja2`
- [ ] §7.2 Prompt Injection — Configurable content filtering, input length limits, output validation
- [x] §7.3 Workflow Bundle Import — Safe YAML parsing (`yaml.safe_load()`) — enforced by semgrep rule `must-use-yaml-safe-load`
- [ ] §7.4 Community Registry — Ed25519 signing with pinned trust anchor (not yet implemented; community library is not live)
- [ ] §7.5 Connector Access Control — owner_id, owner_team_id, visibility, allowed_operations
- [x] §7.6 HITL Claim — Atomic lock via `UPDATE ... WHERE account_id IS NULL AND decision IS NULL RETURNING`
- [x] §7.7 Inbound Webhook Security — HMAC-SHA256 with timestamp replay window (`_verify_hmac` in trigger_engine)
- [ ] §7.8 Outbound Webhook Signing — HMAC-SHA256 per-endpoint signing (notifier has `X-Modulo-Signature`, needs BDD coverage)
- [ ] §7.9 TLS — Reference Caddy configuration, deployment guide notes
- [ ] §7.11 GitHub Connector OAuth Scopes — Minimum scopes, health check verification
- [ ] §7.12 LangGraph Checkpoint Data — Plaintext known gap, V2 Fernet encryption planned
- [ ] §7.14 Team Visibility — Server-side enforcement at ViewModel command layer (RBAC middleware exists, ViewModel layer pending)
- [ ] §7.15 MCP Scope Enforcement — Dual-layer (token middleware + ViewModel command)
- [ ] §7.16 SSE Per-Event Org Context Validation — Every event validated, not just connection
- [x] §7.17 DOM Sensitive Data Rule — Masked default, server-authenticated 30s reveal (sensitive_mask.py + reveal endpoint + BDD tests)
