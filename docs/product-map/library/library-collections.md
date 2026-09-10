---
id: feat-library-collections
prd: N/A
adr: []
code:
  - backend/src/modulo/core/library_service/install.py
  - backend/src/modulo/core/library_service/uninstall.py
  - backend/src/modulo/core/library_service/grant.py
  - backend/src/modulo/core/library_service/runnability.py
  - backend/src/modulo/api/routes/library.py
unit-tests:
  - backend/tests/unit/api/test_library_collection.py
  - backend/tests/integration/test_library_collection_lifecycle.py
depends-on:
  - feat-library
  - feat-pipelines
  - feat-schemas
status: covered
---

# Library Collections

Library collections (FAR-760 / FAR-762 / FAR-764) let an org author a
`library_collection` primitive whose manifest pins schemas, agents, and workflows,
then install it into runnable org entities. Collection authoring lives at
`/library/collections/new` and the detail view at `/library/collections/:id`, both
gated behind the `library_collection` feature flag.

## Behaviours

- [x] Install resolves manifest pins to visible org primitives, materialises
      entities via `materialize_import` (with `format_version` set so the bundle
      passes validation), stamps `collection_install_id` on every created entity
      (schemas, agents, pipelines), and writes a `CollectionInstall` provenance
      record with `status=installed`
      (`core/library_service/install.py`, `test_library_collection_lifecycle.py`)
- [x] Uninstall deletes unmodified entities (matching `collection_install_id`) and
      detaches provenance from modified entities (already-cleared
      `collection_install_id`); the `CollectionInstall` record and entity tracking
      rows are deleted (`core/library_service/uninstall.py`)
- [x] Re-install of the same version is idempotent: missing entities are
      re-added via `materialize_import` without duplicating existing ones
      (`test_library_collection_lifecycle.py`)
- [x] `compute_runnable` is a pure read-time check: `True` when every
      `connector_checklist` entry is `configured+bound` and the org has at least
      one active model backend, `False` otherwise
      (`core/library_service/runnability.py`)
- [x] Community-sourced or registry-sourced collections restrict agent
      tool/connector access until an operator explicitly grants access via
      `grant_collection_agents` (sets `agents_granted=True`, idempotent);
      non-community (`source=local`) collections skip the gate and agents are
      immediately usable (`core/library_service/grant.py`)
- [x] Each unique `connector_type_id` referenced by any agent in the collection
      gets a `connector_checklist` entry with `status=pending`; the runnability
      check verifies all entries are `configured+bound`
      (`core/library_service/install.py`)

## Known Gaps

- **No BDD feature file** — collection install/uninstall/grant is covered by the
  `backend/tests/integration/test_library_collection_lifecycle.py` lifecycle suite
  rather than a `backend/tests/bdd/features/library/` scenario.

## QA History

- 2026-09-10: **Branch Fixer** — registered `feat-library-collections` in the
  manifest, the graph-root registry index, and this behaviour tracker so the
  product-map architecture guards (route reference, registry index enumeration,
  behaviour tracking) stay green.
