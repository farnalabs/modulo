---
id: feat-remy-context-sources
prd: 8.29, 8.30
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/remy/remy_context_sources.feature
unit-tests: []
code:
  - backend/src/modulo/db/models/remy_context_source.py
  - backend/src/modulo/api/routes/remy.py
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

- [x] Feature file covers context source lifecycle (create, list, update, delete)
- [ ] Product primer auto-generation (always-on `## Product Overview` section)
- [ ] Graduated injection modes (always-on, on-demand, excluded)
