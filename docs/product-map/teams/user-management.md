---
id: feat-teams-user-management
prd: 9
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/users/
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
