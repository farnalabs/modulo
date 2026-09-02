# ADR 004 — User Offboarding Uses Deactivation (Not Hard Deletion)

**Date**: 2026-06-29
**Status**: Accepted

---

## Context

The admin API includes an endpoint to `POST /api/v1/admin/users/{user_id}/deactivate`, which sets `active = False` on the user, blacklists all JWT token families, revokes all active API keys, and removes the user from all teams. There is no corresponding `DELETE /api/v1/admin/users/{user_id}` endpoint. The question arose: should admins be able to permanently delete user accounts?

## Decision

**Do not implement hard-deletion of users. Deactivation is the correct and complete offboarding mechanism.**

## Rationale

1. **Audit trail integrity** — Modulo users own audit events, pipeline runs, trigger events, and other history. Hard-deleting a user would orphan these rows or require cascade-deleting historical data, breaking the audit trail. Modulo is building toward SOC 2 compliance (§8.12 of the PRD), which requires an immutable record of who did what — that includes the user's identity at the time of the action. A deactivated user retains their identity in historical records; a deleted user creates a gap.

2. **Foreign key referential integrity** — `User.id` is referenced as `created_by` and `updated_by` in pipelines, runs, schemas, library primitives, connectors, and many other entities. Cascading deletes would silently destroy customer data. Nullifying the FK would lose the identity link. Neither is acceptable.

3. **Reactivation path** — A deactivated user can be reactivated by an admin (`POST /users/{user_id}/reactivate`), restoring their access without data loss. This is important for legitimate scenarios (contractor re-engagement, role change, accidental deactivation).

4. **Session revocation completeness** — The deactivation endpoint already does everything security-sensitive that deletion would do: it invalidates all active sessions (token family blacklist), revokes all API keys, and removes team memberships. From a security standpoint, the user is immediately locked out of the system with no remaining access paths.

5. **GDPR right to erasure** — When GDPR data erasure is required, it should be a separate explicit workflow (data export + entity anonymisation or deletion by a dedicated admin tool), not conflated with the normal offboarding flow. This is consistent with the SOC 2 approach of separating "operational offboarding" from "data privacy erasure."

## Consequences

- The admin UI exposes "Deactivate" and "Reactivate" actions, not "Delete user"
- When GDPR erasure is needed, a separate `DELETE /api/v1/admin/users/{user_id}` endpoint may be added later that specifically handles data anonymisation and relationship cleanup, but it is out of scope for the current offboarding flow
- The deactivation action is recorded in the audit log as an admin action

## Alternatives Considered

### Hard delete with cascade

Rejected — destroys audit trail and customer data. Violates SOC 2 requirements.

### Hard delete with FK nullification

Rejected — loses identity link in historical records. Effective audit requires knowing which user performed an action.

### Soft delete (deleted_at timestamp)

Rejected as unnecessary complexity — the `active` boolean already separates "currently active" from "offboarded" users. A `deleted_at` field would add query complexity without meaningful benefit. The PRD user model (§9.1) already defines `active` as the canonical status field.

## Update (2026-09-02, FAR-533 / gh-1794): deactivation is PER-ORG

The deactivation described above was account-global: `accounts.active = false`
locked the user out of EVERY org they belong to, while the caller was only ever
authorised by a SINGLE shared org. Migration `0173_per_org_deactivation`
redefined the caller-bound SECURITY DEFINER so the org-admin deactivation path
tombstones the caller's-org membership (`org_memberships.deactivated_at`) and
leaves `accounts.active` untouched — a user shared with orgs B/C keeps their
access there. Reactivation (`POST /users/{user_id}/reactivate`, and
`PUT /users/{user_id}` with `is_active`) clears the caller's-org tombstone
only. The session-revocation guarantee of rationale 4 is preserved: token
families and API keys scoped to the caller's org are revoked atomically, and
the tombstone gates login org-resolution, live-role revalidation and refresh
(ADR 017). The global `accounts.active` flip is now reserved for the operator /
break-glass path (`modulo_breakglass`), which remains a global ban by design.
Accounts deactivated under the old global behaviour stay globally inactive
until an operator reactivates them.
