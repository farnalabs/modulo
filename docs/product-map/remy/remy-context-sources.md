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
- [x] Product primer auto-generation (always-on `## Product Overview` section) — `skill_loader.py` injects `## Product Overview\n\n{config.product_primer}` when the `product_primer` context source mode is `always_on` and the primer string is non-empty; covered by `test_product_primer_included_when_always_on`, `test_product_primer_skipped_when_off`, `test_product_primer_skipped_when_empty` and the `remy_context_sources.feature` scenario
- [x] Graduated injection modes (always-on, tool, off)

## QA History

- 2026-08-15: Coverage sweep (partial-small-b). Verified product-primer auto-generation is implemented and tested (`skill_loader.py` + 3 unit tests + BDD scenario); marked `[ ]`→`[x]`. 6/6 behaviours covered.
