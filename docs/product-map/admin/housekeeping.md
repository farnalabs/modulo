---
prd: 0.0
id: feat-admin-housekeeping
delivery-tasks: [task-housekeeping-admin]
bdd: []
unit-tests: []
code:
  - backend/src/modulo/core/housekeeping.py
  - backend/src/modulo/api/routes/admin_housekeeping.py
  - backend/src/modulo/api/mcp_server.py
  - frontend/src/views/AdminHousekeepingView.vue
depends-on: []
status: gap
---

# Housekeeping Admin Page

Admin page for scanning and cleaning up unused/orphaned resources.

## Behaviours

### Scan
- [ ] GET /api/v1/admin/housekeeping returns 8 categories of cleanup candidates
- [ ] Results are org-scoped (RLS enforced)
- [ ] Only org admins can access the endpoint
- [ ] Each candidate has id, name, entity_type, detail, created_at

### Cleanup
- [ ] POST /api/v1/admin/housekeeping/cleanup deletes selected candidates
- [ ] Deletions are grouped by entity type with savepoints
- [ ] FK violations on one type do not block other types

### MCP
- [ ] list_housekeeping tool returns scan results (runner scope)
- [ ] perform_housekeeping tool deletes selected items (operator scope)

### UI
- [ ] Housekeeping page accessible under Monitoring sidebar group
- [ ] Select-all with indeterminate state
- [ ] Category-level checkboxes with indeterminate state
- [ ] Individual item checkboxes
- [ ] Confirmation dialog with type-summary breakdown
- [ ] Empty state when no candidates found
- [ ] Error state with retry button

## Known Gaps
- No unit tests yet
- No BDD feature files yet
- No PRD section for housekeeping
