---
id: feat-infra-security
prd: 7
delivery-tasks: []
bdd: []
code: []
unit-tests: []
depends-on: []
status: partial
---

# Security Controls

Cross-cutting security controls documented across PRD §7. Includes sandboxed Jinja2 rendering, prompt injection guards, workflow bundle import safety, Ed25519 signing for community registry, connector access control, HITL atomic lock, inbound webhook HMAC, outbound webhook signing, TLS enforcement, LangGraph checkpoint data protection, team visibility server-side enforcement, MCP dual-layer scope enforcement, SSE per-event org context validation, and DOM sensitive data masking. These are enforced by semgrep rules, runtime guards, and pre-commit hooks — not by individual route handlers.

Individual subsections (7.10 JWT, 7.13 Secrets, 7.18 Rate Limiting) have dedicated product map entries with code/test references. This entry covers the cross-cutting security posture as a whole.

## Behaviours

- [ ] §7.1 Template Rendering — Sandboxed Jinja2 (`jinja2.sandbox.SandboxedEnvironment`)
- [ ] §7.2 Prompt Injection — Configurable content filtering, input length limits, output validation
- [ ] §7.3 Workflow Bundle Import — Safe YAML parsing (`yaml.safe_load()`)
- [ ] §7.4 Community Registry — Ed25519 signing with pinned trust anchor
- [ ] §7.5 Connector Access Control — owner_id, owner_team_id, visibility, allowed_operations
- [ ] §7.6 HITL Claim — Atomic lock via `UPDATE ... WHERE claimed_by IS NULL RETURNING`
- [ ] §7.7 Inbound Webhook Security — HMAC-SHA256 with timestamp replay window
- [ ] §7.8 Outbound Webhook Signing — HMAC-SHA256 per-endpoint signing
- [ ] §7.9 TLS — Reference Caddy configuration, deployment guide notes
- [ ] §7.10 JWT Security — 15-min access tokens, 7-day refresh, WebSocket ws-token, algorithm pinning
- [ ] §7.11 GitHub Connector OAuth Scopes — Minimum scopes, health check verification
- [ ] §7.12 LangGraph Checkpoint Data — Plaintext known gap, V2 Fernet encryption planned
- [ ] §7.13 Secrets Management — Fernet encryption, credential-in-state rule, startup key enforcement
- [ ] §7.14 Team Visibility — Server-side enforcement at ViewModel command layer
- [ ] §7.15 MCP Scope Enforcement — Dual-layer (token middleware + ViewModel command)
- [ ] §7.16 SSE Per-Event Org Context Validation — Every event validated, not just connection
- [ ] §7.17 DOM Sensitive Data Rule — Masked default, server-authenticated 30s reveal
- [ ] §7.18 API Rate Limiting — Per-endpoint limits with 429 + retry-after
