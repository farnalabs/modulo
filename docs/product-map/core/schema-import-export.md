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

## Error Handling

- [x] Reject invalid JSON → 400
- [x] Reject non-object input → 400
- [x] Reject invalid JSON Schema → 422
- [x] Missing schema returns 404 (via standard GET)
- [x] ProgrammingError returns 501
- [x] SQLAlchemyError returns 503
- [x] Exception returns 500 with logging
- [ ] Import of very large schemas — no size limit enforced

## Edge Cases

- [x] Empty JSON object `{}` — parsed as schema with no properties
- [x] Schema with only `type: object` and no `properties` — handled
- [x] Circular `$ref` references — parsed but may cause recursion in field extraction
- [x] Non-ASCII field names — stored and returned as-is
- [ ] Draft 2020-12 validation rejects valid older drafts (2019-09, draft-07, draft-04)
- [ ] `$ref` to external URLs — not resolved, reference stored as-is

## Security

- [x] Auth required — 401 for unauthenticated
- [x] Schema access is org-scoped — cross-org access returns 404
- [ ] Imported schema content not sanitised — potential XSS in `title`/`description` fields rendered in frontend

## Known Gaps

### 2026-07-12 — Round 3 (systemic sweep: B904, exc_info, dead code)
- No code issues found in entry code paths (schemas.py clean from earlier passes)
- Frontmatter valid; Known Gaps remain accurate
