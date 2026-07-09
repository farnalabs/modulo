---
id: feat-pipelines-composite-templates
prd: 8.24
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/composites/composite_crud.feature
  - backend/tests/bdd/features/composites/composite_library.feature
  - backend/tests/bdd/features/composites/composite_mapping.feature
  - backend/tests/bdd/features/composites/composite_runtime.feature
unit-tests: []
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
