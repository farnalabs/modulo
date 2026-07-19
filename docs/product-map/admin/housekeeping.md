---
prd: 10.4
id: feat-admin-housekeeping
delivery-tasks: [task-housekeeping-admin]
bdd: []
unit-tests:
  - backend/tests/unit/mcp/test_scope_validator.py
code:
  - backend/src/modulo/core/housekeeping.py
  - backend/src/modulo/api/routes/admin_housekeeping.py
  - backend/src/modulo/api/mcp_server.py
  - frontend/src/views/AdminHousekeepingView.vue
depends-on: []
status: partial
---

# Housekeeping Admin Page

Admin page for scanning and cleaning up unused/orphaned resources.

## Behaviours

### Scan
- [X] GET /api/v1/admin/housekeeping returns 8 categories of cleanup candidates
- [X] Results are org-scoped (RLS enforced)
- [X] Only org admins can access the endpoint
- [X] Each candidate has id, name, entity_type, detail, created_at

### Cleanup
- [X] POST /api/v1/admin/housekeeping/cleanup deletes selected candidates
- [X] Deletions are grouped by entity type with savepoints
- [X] FK violations on one type do not block other types
- [X] Batch size limit enforced (500 max)

### MCP
- [ ] list_housekeeping tool returns scan results (operator scope)
- [ ] perform_housekeeping tool deletes selected items (operator scope)

### UI
- [X] Housekeeping page accessible under Monitoring sidebar group
- [X] Select-all with indeterminate state
- [X] Category-level checkboxes with indeterminate state
- [X] Individual item checkboxes
- [X] Confirmation dialog with type-summary breakdown
- [X] Empty state when no candidates found
- [X] Error state with retry button
- [X] Cleanup feedback shown (deleted count and errors)
- [X] Auto-refetch on re-navigation (onMounted)

## Known Gaps
- No BDD feature files yet
- No PRD section for housekeeping
- Scanner failures return empty categories indistinguishable from clean scans
