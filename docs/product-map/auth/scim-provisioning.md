---
id: feat-auth-scim
prd: 9.2, 9.4
delivery-tasks: [task-nv1-team-api-keys]
bdd: backend/tests/features/scim/scim_provisioning.feature
code:
  - backend/src/modulo/api/routes/scim.py
  - backend/src/modulo/db/crud/scim.py
  - backend/src/modulo/auth/scim_auth.py
unit-tests:
  - backend/tests/unit/scim/test_scim_provisioning.py
  - backend/tests/unit/scim/test_scim_provisioning_bdd.py
  - backend/tests/bdd/steps/test_scim_provisioning.py
depends-on: [feat-auth-team-api-keys, feat-teams-team-crud]
status: partial
---

# SCIM Provisioning (SCIM 2.0)

Maps SCIM Users → internal User, SCIM Groups → internal Team + TeamMembership.
Authenticated via `MODULO_SCIM_TOKEN` (shared Bearer token), gated by `MODULO_LICENSE_KEY`.
SCIM-provisioned users get `org_role="runner"` and `auth_provider="scim"` by default.
SCIM-provisioned groups map to Team entities; memberships map to TeamMembership.

## Behaviours

### Service Provider Config
- [x] `GET /ServiceProviderConfig` → 200, valid SCIM config schema with patch supported, bulk not supported, filter supported (maxResults 100), sort not supported
- [x] `GET /ServiceProviderConfig` without valid license → 402
- [x] `GET /ServiceProviderConfig` without valid SCIM token → 401

### Auth — SCIM Token
- [x] Request without Bearer token → 401 (HTTPBearer returns None, handler raises 401)
- [x] `MODULO_SCIM_TOKEN` not set → 501 Not Implemented
- [x] Invalid Bearer token → 401 Unauthorized (HMAC compare)
- [x] Valid token, `MODULO_SCIM_DEFAULT_ORG_ID` is invalid UUID → 500 Internal Server Error
- [x] Valid token, no `MODULO_SCIM_DEFAULT_ORG_ID`, no organisations in DB → 500 Internal Server Error
- [ ] Valid token, `MODULO_SCIM_DEFAULT_ORG_ID` set → principal resolves to that org
- [ ] Valid token, no `MODULO_SCIM_DEFAULT_ORG_ID` → principal resolves to first org by creation date

### License Gate
- [x] All endpoints without `MODULO_LICENSE_KEY` → 402 Payment Required
- [x] All endpoints with valid `MODULO_LICENSE_KEY` → request proceeds

### Users — Happy Path
- [x] Create user → 201, valid SCIM User schema with id, meta, userName, name, emails, active
- [x] Create user with name (formatted, givenName, familyName) → parsed correctly
- [ ] Create user with emails array → stored; primary flagged (gap: emails not stored on Account model, only email field)
- [x] Get user by id → 200, full SCIM User schema
- [x] Get user by nonexistent id → 404 with SCIM Error schema
- [x] List users → 200, SCIM ListResponse with totalResults, itemsPerPage, startIndex, Resources
- [ ] List users with pagination (startIndex, count) → correct offset and limit
- [x] PUT (full replace) → 200, all attributes updated to request values
- [x] PUT nonexistent user → 404
- [x] PATCH `replace` active → 200, active state flipped
- [x] PATCH `replace` userName → 200, email updated
- [ ] PATCH `replace` name → 200, display_name updated
- [x] PATCH `remove` active → 200, active set to false
- [x] PATCH nil (no matching ops) → 200, no changes
- [x] Delete user → 204, user hard-deleted from DB (currently hard delete, not soft deactivate)

### Users — Edge Cases
- [x] Duplicate `userName` → 409 Conflict with SCIM Error schema
- [ ] Create with empty `userName` → 422 validation error (Pydantic)
- [ ] `externalId` in create request → accepted in model but not persisted to User table (gap)
- [ ] `externalId` in PUT request → accepted in model but not persisted (gap)
- [ ] PUT after PATCH → full replace semantics respected (PATCH changes overwritten by PUT)
- [ ] PATCH multi-op, one fails → transaction rolls back, no partial persist (SQLAlchemy session.flush on whole block within transaction)
- [ ] PATCH `add` on existing userName field → overwritten per RFC 7644
- [ ] PATCH `add` on existing active field → overwritten
- [ ] PATCH `remove` on nonexistent path (not "active") → no-op, not error
- [ ] PATCH `add` with value dict containing unknown keys → ignored silently
- [x] PATCH unsupported op string → ignored silently (no validation)
- [x] Filter: `userName Eq "foo"` → matched via ILIKE (LIKE-based, not proper SCIM filter parser)
- [ ] Filter: any expression beyond simple LIKE → silently returns empty (filter parser not implemented)
- [ ] Filter: `active eq true` → treated as substring match on raw filter string, not boolean filter (gap)
- [ ] Filter: unsupported filter syntax → 400 with SCIM Error schema (currently not validated — raw string passes through)
- [ ] Concurrent create of same userName → exactly one 201, rest 409 (unique constraint)
- [ ] `startIndex` = 1 → first page
- [x] `startIndex` > total results → empty Resources, totalResults accurate
- [x] `count` = 0 → 422 validation error (FastAPI Query ge=1 rejects)
- [x] `count` > 100 → 422 validation error (FastAPI Query le=100 rejects)
- [ ] `count` = 1 → single result returned
- [ ] `count` omitted → defaults to 20
- [ ] `startIndex` omitted → defaults to 1
- [ ] Request body has `active: false` on create → user created as inactive
- [ ] No `name` in create request → display_name defaults to userName
- [ ] No `emails` in create request → display_name derived from userName parts (no email stored)
- [ ] SCIM-provisioned user has `org_role="runner"`, `auth_provider="scim"`, `password_hash=None`

### Groups — Happy Path
- [x] Create group → 201, valid SCIM Group schema with id, meta, displayName, members
- [x] Create group with members by valid user UUID → members returned in response
- [x] Get group by id → 200, members resolved with $ref links
- [x] Get group by nonexistent id → 404 with SCIM Error schema
- [x] List groups → 200, SCIM ListResponse with totalResults
- [ ] List groups with pagination → correct offset and limit
- [x] PUT replaces displayName AND members → old members removed, new added within single transaction
- [ ] PUT with same displayName → group name updated
- [x] PATCH `add` member by valid user UUID → member added
- [x] PATCH `add` members as array → all members added
- [x] PATCH `remove` member by `members[value eq "uuid"]` path → member removed
- [x] PATCH `remove` member by value dict → member removed
- [x] PATCH `remove` members by value array → all specified members removed
- [x] Delete group → 204, group and all TeamMembership records removed (cascade)
- [x] Delete nonexistent group → 404

### Groups — Edge Cases
- [x] Duplicate `displayName` → 409 Conflict
- [ ] Group `externalId` in request → accepted in model but not persisted (gap)
- [x] PUT with empty members array → all existing members removed, group kept
- [ ] PUT with nonexistent member user UUID → silently skipped (no 404, no error)
- [ ] PUT with invalid member UUID string → silently skipped (ValueError caught)
- [x] PATCH `replace` members → all existing members removed, new members added within transaction
- [ ] PATCH `add` duplicate member → idempotent (existing membership check returns existing record)
- [ ] PATCH `remove` non-member → no-op, not error (membership not found returns False)
- [ ] PATCH `add` member that doesn't exist as User → silently skipped (user lookup returns None)
- [ ] PATCH `remove` with `path="members[value eq "invalid-uuid"]"` → ValueError caught, silently skipped
- [ ] PATCH `remove` with value dict for non-existent `value` key → TypeError avoided by isinstance check
- [ ] PATCH `replace` displayName → updates group name via scim_update_group
- [ ] PATCH `replace` with empty body → no-op, group unchanged
- [x] PATCH with unsupported op string → ignored silently
- [ ] Filter: `displayName Eq "Engineering"` → matched via ILIKE on Team.name
- [ ] Filter: complex expression beyond LIKE → silently returns empty
- [ ] Group `created_by` set to first org user; if no org users → uuid zero placeholder
- [ ] Concurrent create of same displayName → exactly one 201, rest 409

### Cross-Cutting
- [ ] RLS isolation: SCIM operations set `SET LOCAL app.organisation_id` from principal org
- [ ] RLS isolation: SCIM provisioned user in org A cannot be returned by org B queries
- [ ] RLS isolation: SCIM provisioned group in org A not visible to org B
- [ ] Team-scoped resources: Group maps to Team entity; TeamMembership records scoped to org
- [x] License key expired/missing → 402
- [x] SCIM token not configured → 501 (separate from license gate)
- [x] SCIM token invalid → 401
- [ ] All SCIM error responses use SCIM Error schema (`urn:ietf:params:scim:api:messages:2.0:Error`) with detail and status
- [ ] IdP sends duplicate PUT within same second → updates applied, no conflict (PUT is idempotent, last-write-wins)
- [ ] SCIM userName with special characters: `user+tag@domain.com` → matched via ILIKE (works with LIKE filter)
- [ ] Re-provisioning after offboarding: user was hard-deleted, IdP re-sends → new user created (current impl: hard delete, no reactivation)
- [ ] `externalId` in request not mapped to internal User schema → not available for matching on re-provisioning (gap)
- [ ] Bulk provisioning: 100 users in rapid succession → no rate limiting, no queuing, all processed concurrently
- [ ] Org mismatch: SCIM token valid but principal org_id differs from targeted data → RLS enforces isolation (no cross-org leak)

## Not implemented (known gaps)
- `/Bulk` endpoint — SCIM 2.0 Bulk operations (Azure AD uses this)
- `/ResourceTypes` endpoint
- `/Schemas` endpoint
- Enterprise User Schema extension (`urn:ietf:params:scim:schemas:extension:enterprise:2.0:User`)
- `externalId` stored on User model and used for re-provisioning matching (currently `externalId` in request body accepted but discarded)
- PATCH `path` attribute grammar validation (free-form `path` string, no schema validation)
- SCIM filter syntax parser (raw string passed to CRUD ILIKE match; silently returns empty on complex filters)
- Rate limiting / IdP backpressure
- Soft-delete deactivation on user DELETE (currently hard delete, not `active=false` as SCIM 2.0 specifies)
- User `org_role` mapping from SCIM attributes (all SCIM users default to `runner`)
- PATCH operation validation: unsupported `op` values silently ignored instead of returning 400
- SCIM User `emails[]` from request not mapped to User model (display_name derived from userName parts only)
- Group `created_by` fallback to uuid zero when no org users exist
- `members[value eq "..."]` remove path regex extraction fragile — malformed paths silently no-op
- **Re-provisioning IDP user**: If user exists in another org (same email), `scim_create_user` adds a new membership but preserves the existing account — this re-membership path is undocumented in spec
- **`_get_base_url` failure**: No fallback if `modulo_public_url` is unset — crashes with AttributeError
- **SCIM bypasses Team CRUD REST validation**: Calls `create_team` directly rather than Team CRUD API — no duplicate name validation at CRUD level beyond DB constraint
