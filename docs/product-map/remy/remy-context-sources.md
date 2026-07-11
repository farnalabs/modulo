---
id: feat-remy-context-sources
prd: 8.29, 8.30
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/remy/remy_context_sources.feature
unit-tests:
  - backend/tests/unit/remy/test_context_source_service.py
  - backend/tests/unit/remy/test_config_service.py
  - backend/tests/unit/remy/test_skill_loader.py
code:
  - backend/src/modulo/api/routes/me.py
  - backend/src/modulo/api/routes/admin_remy.py
depends-on: [feat-remy-assistant]
status: partial
---

# Remy Context Sources — Configurable Knowledge Domains

Remy's knowledge is organised into context sources — named domains of information that can be independently configured for injection, on-demand retrieval, or exclusion.

## Behaviours

### Model

- [x] `RemyContextSource` model with name, description, source_type, config JSON
- [x] Org-scoped via OrgScoped base
- [x] CRUD integrated into Remy API routes

### BDD Tests

- [x] Feature file covers source modes, user overrides, skill filtering, and MCP tools
- [ ] Product primer auto-generation (always-on `## Product Overview` section)
- [x] Graduated injection modes (always-on, tool, off)
