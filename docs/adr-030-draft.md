# ADR 030 -- User-scoped MCP keys: per-user key issuance; MCP calls operate under the key owner's identity

> **CONDUCTOR NOTE:** This file is a TRANSPLANT DRAFT. ADRs live in
> `Repos/devtools/adr/` (migrated out of modulo in FAR-434, where they are
> numbered by sequence -- this lands as `030-user-scoped-mcp-keys.md`,
> cross-referencing ADR 014/017/018). When transplanting, also apply the
> one-line reciprocal edit to ADR 017 named in the amendment below.

**Status:** Accepted (Phase 1 mechanism shipped; FAR-620)
**Date:** 2026-09-06
**Related:** ADR 014 (MCP server as agents), ADR 017 (centralized authorization, Founder Decision 1), ADR 018 (duplicate numbering noted; the room's ADR sequence has two 018s; this file does not renumber), FAR-602 (HITL email alerts), FAR-614 (user-scoped MCP preference tools)

## Context

Modulo's MCP server (ADR 014/017) is the control plane for external agents,
Claude Code, IDE agents, Remy sessions, CI automation. Today every MCP API
key is an ORG-LEVEL credential: it carries an org role (operator/runner), a
team boundary, and optionally a run binding (sandbox keys), but it has no
identity axis. Consequences:

- A service key minted by one person can alter any user's profile-level
  configuration (today: the dashboard notification level; with FAR-602's
  HITL email preferences, tomorrow: anyone's email alerting).
- User-scoped actions (FAR-614: get/set the CALLER's OWN HITL email-alert
  preference) have no safe credential class: mail-merge automation wants a
  long-lived, headless credential that acts as the USER, not as a
  shared org service identity.

Design principle (Duncan, 2026-09-04): **an org-scoped MCP service key must
NEVER alter user-level configuration for any user.** The mechanism is a
user-level key that acts as its creator in all instances. User-scoped
creation is the DEFAULT posture; org-wide is the deliberate opt-in
(machine identities: CI/CD, external automation, A2A).

## Decision

1. **New `scope` column on `org_api_keys`** (`'org' | 'user'`), NOT NULL,
   server default `'org'`, CHECK mirroring the role CHECK nested DDL.
   IMMUTABLE post-mint (update payloads reject an explicit `scope` with 422;
   `update_api_key` accepts no scope parameter). The historical `OrgApiKey`
   table name is acknowledged; the org-key mint path now defaults the
   column, it is not renamed.

2. **Caller-scope classification (3-value map beside
   `TOOL_SCOPE_REQUIREMENTS`)**: `org-only | caller-scoped | any`. Pure
   resolver: `resolve_tool_access(tool, action, role, key_scope, auth_type,
   allowed_tools, kill_switch)`. `check_tool_scope` delegates to it.

3. **The `.self` suffix derives caller-scoped.** A tool whose permission key
   ends in `.self` operates on the CALLER'S OWN account; there is no parallel
   classification set to keep in sync. `hitl_email.self` @ viewer is the
   first such key (FAR-614 tools: `get_hitl_email_alerts`,
   `set_hitl_email_alerts`).

4. **Claims of identity:** the target of a caller-scoped tool is
   `_ctx_user_id_val()` BY CONSTRUCTION, the no-target-param invariant.
   Registry-introspection tests pin the allowed-parameters map for the
   new/changed tools so no target field can be introduced silently.

5. **`create_api_key` MCP tool stays ORG-ONLY.** Minting is an org-level
   operation; user-scoped key minting is REST-JWT-only (single-tenant
   dogfood: one surface, one matrix cell, no scope-conditional branch in
   the tool body). Under a user-scoped key the MCP mint tool is DENIED.

6. **All `Account.preferences` writes are row-locked** through the shared
   helpers in `db/crud/account.py` (`set_hitl_email_preference` is the
   SINGLE writer of the `hitl_email` key; `update_account_preferences` takes
   the FOR UPDATE lock and merges per-top-level-key). Begin-agnostic; both
   the REST DI session (autobegin=False) and the MCP `_session` wrapper
   (caller's `s.begin()`) work identically. The 365-day-old lost-update race
   documented in me.py is closed. Accepted debt: `Account.preferences` is a
   single-tenant-per-user JSON document in ONE COLUMN shared across orgs
   (accounts are global), a cross-org inherited preference is possible on
   multi-org accounts; accepted for this dogfood deployment.

7. **Run attribution.** Manual runs are stamped with the account of the
   authenticating credential; MCP `_create_manual_run` and REST
   `POST /api/v1/runs` now pass `account_id`; the trigger test path already
   did (`triggers.py`). Attribution semantics: `run.account_id` = the
   account of the credential, NOT the human operator behind it. Webhook /
   cron / agent_signal child runs remain legitimately NULL (pinned invariant
   test kept green).

8. **Rate buckets.** The in-app `trigger_pipeline` limiter buckets
   user-scoped keys as `user:{account_id}` (a user-scoped key is one client);
   org/team/run-scoped keys keep `ak:{key_id}` (the org-key multiplication
   hole is pre-existing and accepted). The middleware bucket stays per-key,
   accepted asymmetry, documented.

9. **Audit.** MCP `create_api_key`/`revoke_api_key` emit
   `api_key_created`/`api_key_revoked` (exact REST event_type strings).
   Payloads on BOTH events on BOTH surfaces gain `auth_type` + `key_scope`
   + masked prefix (`mk_<prefix>****`). Adjacent, out-of-scope:
    admin regenerate key lacks audit (own ticket); the audit-viewer filter
    drifted from the event types (`api_key.created`/`deleted` vs
    `api_key_created`/`revoked`) -- pre-existing defect, untouched.

## The ceiling is REUSE, not amendment

ADR-017's mint ceiling is unchanged: `min(minted role, live role)` per
call, resolved from live membership. User-scoped keys never exceed
operator. Runner key + live viewer DEGRADES to viewer (min(1,0), degraded,
not dead); death happens only on missing/deactivated membership. The
live-role clamp makes an expires_at extension (TTL risk below) tolerable;
a stale key cannot out-live its owner's live role.

| Minted | Live role | Result |
|--------|-----------|--------|
| operator | operator/operator | full |
| runner | viewer | **degrades to viewer** (test-pinned cell) |
| any | None (removed/deactivated) | dies (401) |

## ADR 017 Founder Decision 1 -- amendment

ADR 017 DECISION 1 reads "Machine clients use org API keys". This ADR
amends it to:

> *Machine clients use org API keys by default; user-scoped keys extend the
> credential model for machine clients that must act AS a user (personal
> agents, per-user automations). Org-wide remains the deliberate opt-in for
> shared service identities.*

**Reciprocal edit for ADR 017 (Conductor lands in devtools alongside this
ADR):** append to DECISION 1, "Amended by ADR 030: user-scoped MCP keys add
a per-user credential class for identity-bound machine clients; org API keys
remain the default for shared machine identities."

## The claimant decision (fail-closed)

`hitl.claim`'s `claimant_id` stays KEY-SCOPED (the original decision, not
changed to user-scoped). Team-gated HITL gates are therefore MCP-unclaimable
by design; an agent credential cannot silently claim for a human. If team
members are gate owners, the gate must be claimed through the REST UI.
This is fail-closed and accepted.

## Why not OAuth for the headless-agent problem

OAuth is the right protocol for interactive browser consent; it cannot
solve headless provisioning: consent flows need a browser, and access
tokens are short-lived by design. A cron-scheduled Remy/job needs a
long-lived credential minted ahead of time that STILL acts as a single
user, does not touch other users' state, and can be revoked independently
without rotating the org's service credentials. A user-scoped key with the
org cookie jar guarantees exactly that, and the live-role clamp +
per-account quota (10 active) + independent revocation are the
compensating controls.

## Bootstrap recipe (first user key)

```bash
# 1. Log in (JWT -- the REST route is JWT-only)
curl -X POST https://app.modulo.run/api/v1/auth/login \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=admin@modulo.run&password=***'
# → {"access_token": "eyJ..."}
TOKEN=eyJ...

# 2. Mint a user-scoped key (flag user_scoped_mcp_keys must be ON for the org)
curl -X POST https://app.modulo.run/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "user:duncan", "role": "operator", "scope": "user"}'
# → {"key_value": "mk_..."}  (shown once; store immediately)
```

## TTL

365-day default expiry inherited from the org-key interim TTL (Phase 2
decides the final policy). Compensating controls that hold TODAY: the
live-role clamp (a stale key cannot out-live its owner's live role), the
per-account active-key quota, and per-key revocation. The named risk:
`expires_at` extension would unbind the credential from those controls;
the update path accepts no scope and the extension risk is deferred to the
Phase 2 TTL policy decision.

## Flag gates and rollback

Org-level `user_scoped_mcp_keys` flag. OFF-state semantics (pinned):
(1) mint of scope='user' is DENIED (never silently downgraded to org);
(2) a STORED user-scoped key is DENIED AT AUTH (401); disabling revokes,
never broadens; (3) JWT/OAuth callers unaffected; (4) org-key behavior is
byte-identical. The caller-scope gate is kill-switch-INELIGIBLE (the
tenant-boundary precedent): an org-wide key stays denied user-state writes
even with authz-enforce off. The flag read failure is fail-closed. Deploy
ordering: old-code + new-column is safe (column is additive with server
default); new-code + old-column fails at the resolver read, model and
migration land in the same PR. Migration downgrade: former user-scoped
keys re-read as 'org', silent widening on rollback is PINNED as accepted
(0126 round-trip template).

## Observability notes

- **Bucket-string shift:** Remy's JWT callers now carry auth_type 'jwt'
  (previously 'oauth'), so the trigger_pipeline bucket string shifts
  `trigger_pipeline:{org}:oauth:user:{uid}` → `...:jwt:user:{uid}`, a
  one-time in-memory budget reset for those callers (fresh empty buckets).
  User-scope API keys likewise newly share the account bucket. Watch 429
  logs across the deploy window and do not treat aggregate 429 dips as a
  rate-limit fix.
- Auth_type 'jwt' is a NEW value distinguishable from OAuth; it cannot by
  itself distinguish OAuth from the old regular-JWT (pre-existing gap), but
  it CAN separate Remy-held JWT sessions from OAuth tokens in audit
  payloads from this release onward.
- The mint-cap / role-cap counters (`permission.api_key_role_cap`)
  continue to count user-scope key clamps exactly like org-key clamps (the
  degrade cell is observable through the same signal).

## Alternatives considered

- **Parallel "user API keys" table**: rejected, a second credential model
  duplicates validation, RLS, mint/quota/audit plumbing; the scope axis
  composes with the existing role + team + run binding axes instead.
- **Greedy org-key user-delegation header (`X-Modulo-Act-As`)**: rejected,
  gives exactly the configuration-forge capability the design principle
  forbids, guarded only by a convention.
- **Per-key user binding on every key** (stamping account on every mint):
  conflates "owned by" with "acts as"; every key is owned by someone, but
  only service keys need the caller scope.
- **OAuth device flow for headless agents**: browser-consent-gated and
  short-lived; does not cover scheduled long-lived agents.
- **JSONB per-key spans of Account.preferences writers** (migration to
  `jsonb_set`-style Merkle updates): no schema migration needed once the
  row lock serialises the blob; 0129's NUL-byte hazard argues against any
  unnecessary change to that column's writer set.
