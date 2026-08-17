---
id: feat-teams-user-management
prd: 9
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/users/basic_auth.feature
  - backend/tests/bdd/features/users/roles.feature
  - backend/tests/bdd/features/users/runner_role.feature
  - backend/tests/bdd/features/teams/admin_override.feature
  - backend/tests/bdd/features/teams/cross_team_isolation.feature
  - backend/tests/bdd/features/teams/stale_jwt_revocation.feature
  - backend/tests/bdd/features/teams/team_create.feature
  - backend/tests/bdd/features/teams/team_crud.feature
  - backend/tests/bdd/features/teams/team_deletion.feature
  - backend/tests/bdd/features/teams/team_deletion_blocked.feature
  - backend/tests/bdd/features/teams/team_hitl_gate.feature
  - backend/tests/bdd/features/teams/team_membership.feature
  - backend/tests/bdd/features/teams/team_pipeline_visibility.feature
  - backend/tests/bdd/features/teams/view_as_team.feature
  - backend/tests/bdd/features/teams/view_as_team_non_admin_rejected.feature
  - backend/tests/bdd/features/auth/change_password.feature
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/features/auth/sso_team_mapping.feature
  - backend/tests/bdd/features/auth/api_keys.feature
  - backend/tests/bdd/features/auth/jwt_security.feature
  - backend/tests/bdd/features/auth/login.feature
  - backend/tests/bdd/features/auth/rbac.feature
  - backend/tests/bdd/features/auth/tenant_isolation.feature
  - backend/tests/bdd/features/system_admin/system_admin_users.feature
code:
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/auth.py
  - backend/src/modulo/api/routes/scim.py
  - backend/src/modulo/api/routes/admin.py
unit-tests:
  - backend/tests/unit/api/test_teams.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/api/test_team_admin_rls_context.py
  - backend/tests/bdd/steps/test_team_deletion.py
  - backend/tests/unit/api/test_team_gating.py
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/bdd/steps/test_sso_team_mapping.py
  - backend/tests/unit/auth/test_api_key.py
  - backend/tests/unit/api/test_auth_rate_limiter.py
  - backend/tests/unit/auth/test_login_endpoint.py
  - backend/tests/unit/auth/test_jwt.py
  - backend/tests/unit/auth/test_passwords.py
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/bdd/steps/test_sso_oidc.py
  - backend/tests/bdd/steps/test_sso_saml.py
  - backend/tests/unit/db/crud/test_team.py
  - backend/tests/unit/db/crud/test_team_membership.py
  - backend/tests/integration/crud/test_team_isolation.py
  - backend/tests/integration/crud/test_user_isolation.py
depends-on:
  - feat-auth-jwt-auth
status: partial
---

# User Management & Access Control

User CRUD, roles (admin/operator/runner), SCIM provisioning, team membership management.

## Behaviours

- [x] Â§9.1 User Model â€” id, email, display_name, org_role, auth_provider, sso_subject, active, organisation_id
- [x] Â§9.2 Roles â€” org-level and team-level: admin, operator, runner
- [x] Â§9.2 SCIM provisioning â€” create/update/delete users and groups via SCIM 2.0
- [x] Â§9.3 Team CRUD â€” create, list, update, delete teams
- [x] Â§9.3 Team membership management â€” add/remove members, role assignment per team
- [x] Â§9.3 Team isolation â€” team-private resources not visible to non-members
- [x] Â§9.3 API keys â€” per-team API key management
- [x] Â§9.4 SSO provider UI â€” configure OIDC/SAML providers
- [x] Â§9.4 Password change â€” local auth password management
- [x] Â§9.4 User offboarding â€” deactivate users, transfer ownership
- [x] Â§9.4 SSO team mapping â€” auto-assign team membership from SSO claims

## Known Gaps

- **Stub file aggregation**: This file serves as a Â§9 index. Most behaviours are tracked in dedicated product map entries: `feat-teams-team-crud` (Â§9.3), `feat-teams-user-offboarding` (Â§9.4), `feat-teams-sso-team-mapping` (Â§9.4), `feat-auth-jwt-auth` (Â§9.1). Verify each behaviour's test coverage against its dedicated entry.
- **SCIM bypasses REST API validation**: SCIM CRUD functions call Team/Account CRUD directly, bypassing REST API validation layer. Any validation gap in the underlying CRUD (e.g. duplicate name enforcement) is inherited by SCIM.
- **SCIM endpoints lack `set_rls_user_context`**: Unlike REST API team endpoints, SCIM routes (`scim.py`) only call `set_rls_org` without `set_rls_user_context`. This is acceptable for SCIM (uses API token auth rather than user auth) but means SCIM operations don't carry per-user audit context.
- **Unit test coverage for auth routes**: Several auth endpoints (`/logout`, `/refresh`, `/me`) lack dedicated unit test files with exception-path coverage. The existing tests cover happy path and some error cases but not all DB exception variants.
