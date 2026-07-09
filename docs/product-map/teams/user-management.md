---
id: feat-teams-user-management
prd: 9
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/users/basic_auth.feature
  - backend/tests/bdd/features/users/roles.feature
  - backend/tests/bdd/features/users/runner_role.feature
code:
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/auth.py
  - backend/src/modulo/api/routes/scim.py
unit-tests: []
depends-on:
  - feat-auth-jwt-auth
status: partial
---

# User Management & Access Control

User CRUD, roles (admin/operator/runner), SCIM provisioning, team membership management.

## Behaviours

- [ ] §9.1 User Model — id, email, display_name, org_role, auth_provider, sso_subject, active, organisation_id
- [ ] §9.2 Roles — org-level and team-level: admin, operator, runner
- [ ] §9.2 SCIM provisioning — create/update/delete users and groups via SCIM 2.0
- [ ] §9.3 Team CRUD — create, list, update, delete teams
- [ ] §9.3 Team membership management — add/remove members, role assignment per team
- [ ] §9.3 Team isolation — team-private resources not visible to non-members
- [ ] §9.3 API keys — per-team API key management
- [ ] §9.4 SSO provider UI — configure OIDC/SAML providers
- [ ] §9.4 Password change — local auth password management
- [ ] §9.4 User offboarding — deactivate users, transfer ownership
- [ ] §9.4 SSO team mapping — auto-assign team membership from SSO claims

## Known Gaps

- **Stub file aggregation**: Most behaviours listed here are tracked in dedicated product map entries: `feat-teams-team-crud` (§9.3), `feat-teams-user-offboarding` (§9.4), `feat-teams-sso-team-mapping` (§9.4), `feat-auth-jwt-auth` (§9.1). This file serves as a §9 index rather than an independent feature entry.
- **No unit test files listed**: `unit-tests: []` because individual behaviours are tested across the dedicated feature's test files. This should be populated with cross-cutting test files that verify user management integration.
- **No delivery tasks linked**: `delivery-tasks: []` because the tasks are distributed across the individual feature entries listed above.
- **All behaviours unchecked**: None have been verified from this file. Each behaviour should be verified against its dedicated product map entry's behaviours section.
