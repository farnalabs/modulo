---
id: feat-core-schema-import-export
prd: 8.3
bdd:
  - backend/tests/bdd/features/schemas/create.feature
unit-tests:
  - backend/tests/unit/api/test_schemas_endpoint.py
code:
  - backend/src/modulo/api/routes/schemas.py
delivery-tasks: []
depends-on: [feat-core-schema-system]
status: partial
---

# Schema Import / Export

Parse raw JSON Schema content and extract fields for use in the schema builder UI. Export happens via the standard GET schema / GET schema version endpoints.

## Behaviours

### Import

- [x] Parse raw JSON Schema content → 200 with name, description, fields
- [x] Reject invalid JSON → 400
- [x] Reject non-object input → 400
- [x] Reject invalid JSON Schema → 422
- [x] Extract fields from properties, mark required fields
- [x] Extract name from `title` field
- [x] Extract description from `description` field

### Export

- [x] Schema detail response includes all fields (via GET /schemas/{id})
- [x] Schema version detail response includes full definition_json (via GET /schemas/{id}/versions/{version})

## Known Gaps

- **No dedicated export endpoint** — export is implicit via standard CRUD reads
- **No bundle export** — no endpoint to export schema + versions as a single bundle
- **Import validation uses Draft 2020-12** — older drafts may produce false validation errors
- **No BDD scenarios for import endpoint** — create.feature covers schema CRUD but not import specifically

## QA History

### 2026-07-12 — Round 3 (systemic sweep: B904, exc_info, dead code)
- No code issues found in entry code paths (schemas.py clean from earlier passes)
- Frontmatter valid; Known Gaps remain accurate
