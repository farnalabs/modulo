---
id: feat-pipelines-composite-templates
prd: 8.24
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/composites/composite_crud.feature
  - backend/tests/bdd/features/composites/composite_library.feature
  - backend/tests/bdd/features/composites/composite_mapping.feature
  - backend/tests/bdd/features/composites/composite_runtime.feature
unit-tests:
  - backend/tests/unit/api/test_composite_templates_api.py
code:
  - backend/src/modulo/api/routes/composite_templates.py
  - backend/src/modulo/db/models/composite_template.py
depends-on: [feat-pipelines-core]
status: partial
---

# Composite Templates

Parameterised pipeline templates that can be instantiated at runtime with user-supplied inputs, including input/output schema mapping and runtime parameter injection.

## Behaviours

### CRUD

- [x] Full CRUD at /api/v1/composite-templates
- [x] Org-scoped RLS
- [x] Validation of parameter ports and mapping

### Library Integration

- [x] Save composite templates as library primitives

### Input/Output Mapping

- [x] Input mapping configuration
- [x] Output mapping configuration

### Runtime

- [x] Parameter injection at expansion time
- [x] Pipeline expansion from template

### Parameter Port Types

- [x] string, number, boolean, select
- [x] model_backend_ref, schema_ref

## Error Handling

- [x] Composite template CRUD routes catch ProgrammingError → 501
- [x] Composite template CRUD routes catch SQLAlchemyError → 503
- [x] Composite template editor routes catch ProgrammingError → 501
- [x] Composite template editor routes catch SQLAlchemyError → 503
- [x] Composite template publish route catches ProgrammingError → 501
- [x] Composite template publish route catches SQLAlchemyError → 503
- [x] Auth 401/403 enforced via Depends(get_current_user)
- [x] 422 validation for missing/invalid fields (name, sub_pipeline_graph_json)
- [x] Non-existent template returns 404 on get/update/delete

## Edge Cases

- [x] Empty name returns 422
- [x] Missing name returns 422
- [x] Missing sub_pipeline_graph_json returns 422
- [x] Unauthenticated requests return 401/403
- [x] Non-existent template ID returns 404

## Known Gaps

- No BDD scenarios for composite template CRUD error paths
- No BDD scenarios for composite template parameter validation at runtime
- No dedicated unit tests for ProgrammingError or SQLAlchemyError on composite template routes (2026-07-11: Added SQLAlchemyError→503 catches to all 7 DB-accessing endpoints. ProgrammingError→501 was already present. Error-path coverage relies on existing test patterns, not dedicated unit tests.)
- No integration tests for composite template expansion with real parameter injection

## QA History

- 2026-07-09: Second-pass product map QA (feat-pipelines-composite-templates): Added unit-tests frontmatter reference (test_composite_templates_api.py covers 16 tests across CRUD + auth). Added Error Handling, Edge Cases, Known Gaps, and QA History sections. Template was previously documented without standard product map sections.
- 2026-07-11: Third-pass QA: Added SQLAlchemyError→503 catches to all 7 DB-accessing endpoints (list, create, get, update, delete, editor get, editor save, publish). Added `populate_by_name=True` to CompositeTemplateResponse model_config. Updated product map Error Handling section with editor/publish routes. Marked SQLAlchemyError gap as resolved in Known Gaps.
