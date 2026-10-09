---
id: feat-scim
prd: 9.4
adr: [ADR 047 (live-role re-read)]
code:
  - backend/src/modulo/api/routes/scim.py
  - backend/src/modulo/auth/scim_auth.py
  - backend/src/modulo/db/crud/scim.py
  - backend/src/modulo/core/feature_flags.py
unit-tests:
  - backend/tests/unit/scim/test_scim_provisioning.py
  - backend/tests/unit/scim/test_scim_provisioning_programming_error.py
  - backend/tests/unit/api/test_scim_route_branch.py
  - backend/tests/unit/auth/test_scim_auth.py
  - backend/tests/unit/db/crud/test_scim.py
bdd:
  - backend/tests/bdd/features/scim/scim_provisioning.feature
depends-on:
  - feat-sso
  - feat-teams-org-entity
status: covered
---

# SCIM 2.0 Provisioning

API-only SCIM 2.0 provisioning surface (`/scim/v2/*`) that lets an Enterprise
identity provider (Okta, Entra ID, ...) push users and groups into Modulo so
team membership syncs automatically from the IdP. There is deliberately **no
UI**: SCIM is a machine-to-machine protocol driven by the IdP, so it is a
backend-only plan feature (`scim`, `tier: team`) and the frontend
`teamFeatureGateParity` suite lists it as a backend-only exception. Auth is a
shared-secret bearer token (`MODULO_SCIM_TOKEN`) rather than a user JWT; the
target org is `MODULO_SCIM_DEFAULT_ORG_ID` when set, else the first-created
org. Every mutation runs under org RLS and is audited fail-closed (ADR 047).

## Behaviours

- [x] Auth + gating: a missing bearer token is 401 `Missing SCIM token`, an
      invalid token is 401 `Invalid SCIM token`, an unconfigured instance
      (`MODULO_SCIM_TOKEN` unset) is 501 `SCIM is not configured`, a malformed
      `MODULO_SCIM_DEFAULT_ORG_ID` is 500, and the whole surface is gated behind
      the `scim` plan feature — a plan without it gets 402
      `scim is not available on your plan`
      (`backend/src/modulo/auth/scim_auth.py`; `test_scim_auth.py`,
      `scim_provisioning.feature`)
- [x] `GET /scim/v2/ServiceProviderConfig` advertises the supported
      capabilities truthfully: `patch` supported, `bulk` / `sort` / `etag` /
      `changePassword` not, `filter` supported with `maxResults: 100`, and a
      Bearer authentication scheme
- [x] `GET /scim/v2/Users` returns a SCIM `ListResponse`
      (`totalResults` / `itemsPerPage` / `startIndex` / `Resources`), paginated
      by `startIndex` (>= 1) and `count` (1–100, default 20) with an optional
      SCIM `filter`; `GET /scim/v2/Groups` mirrors it for teams
- [x] `POST /scim/v2/Users` JIT-provisions a Modulo user: a brand-new account is
      created with `auth_provider="scim"`, a NULL `password_hash` (a
      password login on a SCIM account is 401 by design) and an org membership
      at the default `runner` role; an existing account without a membership is
      attached, and a tombstoned (soft-deleted) membership is REVIVED
      (tombstone cleared, account re-activated) so an IdP delete-then-recreate
      is reversible rather than a permanent 409; a duplicate ACTIVE membership
      is 409 (`backend/src/modulo/db/crud/scim.py`; `test_scim_provisioning.py`)
- [x] `GET` / `PUT` / `PATCH` / `DELETE /scim/v2/Users/{user_id}`: reads are
      404 for an unknown id; `PUT` replaces attributes; `PATCH` applies
      add/replace/remove operations; a `PATCH active=false` (or a `PUT`/`PATCH`
      that clears `active`) DEPROVISIONS via the caller-bound
      `deactivate_break_glass` SECURITY DEFINER — the membership is tombstoned
      and record-preserving, never hard-deleted — and `DELETE` applies the same
      tombstone (`scim_provisioning.feature` "Delete ... deactivates",
      "Deprovisioning ... preserves the record")
- [x] Deprovisioning fails closed around the last-admin invariant: SCIM
      authenticates with a shared token and has no per-user identity, so the
      acting caller is the org's first active non-break-glass admin; when none
      exists the delete is 409 `No active admin exists ...`, a last-admin
      lockout is 409 with the guard's reason, and an unverifiable guard is 503
      (never a silent success that locks the org out)
      (`assert_not_last_admin`, `_resolve_scim_admin_caller`)
- [x] `POST /scim/v2/Groups` maps an IdP group to a Modulo team (duplicate
      `displayName` → 409) and syncs its initial members; `PATCH`/`PUT`
      `/scim/v2/Groups/{group_id}` add/remove members and rename the team, and
      the member operations are validated against the caller's org so a group
      update can never attach a cross-tenant user (cross-org membership hole
      closed); `DELETE` removes the team
      (`backend/src/modulo/db/crud/scim.py`; `test_scim_route_branch.py`,
      `test_scim.py`)
- [x] Every user/group mutation is audited fail-closed
      (`scim_user_created` / `scim_user_updated` / `scim_user_deleted` and the
      group equivalents; FAR-1472), attributed to that same resolved admin
      authority — a provisioning change that lands without an audit event fails
      the response rather than mutating membership unattributably
- [x] Database failures on the mutating routes map to honest SCIM status codes
      instead of a generic 500: `ProgrammingError` (missing migration) → 501,
      `IntegrityError` → 409 and other `SQLAlchemyError` → 503 — except a
      session-contract violation (`InvalidRequestError` / `MissingGreenlet`),
      which `raise_session_contract_error` classifies to 500 rather than a
      retry-inviting 503. The `GET` list routes are the exception: their CRUD
      helper swallows `ProgrammingError` and returns an empty collection (200),
      so the route-level 501 arm is unreachable for a real query failure
      (`scim_provisioning.feature`, `test_scim_provisioning_programming_error.py`)

## Known Gaps

- **No `PATCH` role-update path** — ADR 047 phase 1 leaves the SCIM user
  role-update intentionally unwired: a SCIM user defaults to the `runner`
  grant and an IdP-driven role change is not propagated. Wiring it needs an
  ADR 047 follow-up.
- **Group membership is org-scoped, not team-scoped** — a SCIM group syncs to
  the caller's org team; there is no per-IdP-group → multiple-team fan-out.

## QA History

- 2026-10-08: **Improve Architecture product-map walk** – tracked the untracked
  SCIM 2.0 provisioning surface as its own `feat-scim` behaviour tracker. The
  full API-only surface (`/scim/v2/ServiceProviderConfig`, Users and Groups
  CRUD, the shared-secret `MODULO_SCIM_TOKEN` auth, the `scim` plan gate, the
  last-admin-safe deprovisioning and the fail-closed mutation audits) shipped
  with executing BDD (`scim_provisioning.feature`) and unit coverage but had NO
  home in either product-map layer — it was invisible to the feature graph and
  to Assistant's `search_documentation` indexer. Corrected the stale
  `feat-sso` deferral that still claimed "SCIM deprovisioning [is] not
  implemented yet": SCIM user deprovisioning (`DELETE /scim/v2/Users/{id}` and
  `PATCH active=false`) is implemented as a record-preserving tombstone, so the
  deferral now names only the genuinely-unshipped SAML single logout.
  `_ORPHANED_BDD_FEATURES` stays empty.
