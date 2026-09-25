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
bdd:
  - backend/tests/bdd/features/library/library_collections.feature
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
- [x] The install/uninstall/grant REST contract (`/api/v1/libraries/collections`):
      installing a published collection returns 201 with an `installed` install
      record + runnability verdict, a non-published collection is 400, an
      unresolvable pin is 422, a repeat install is 400; uninstall returns the
      deleted/detached split (modified entities detached, unmodified deleted,
      unknown install 404); grant flips `agents_granted` for community-sourced
      installs (200, idempotent when already granted), 400 for local installs,
      404 for unknown installs (`library_collections.feature`,
      `steps/test_library_collections.py`)
- [x] The authoring REST contract has executing BDD coverage too: creating a
      collection returns 201 with `status=draft` and echoes the manifest pins,
      a duplicate slug is 409; updating a draft replaces its pins (200) while
      updating a published collection is 400; publishing a draft collection
      validates pins — valid pins publish (200), an empty manifest / duplicate
      pins / more than the 25-pin cap / an unknown pinned primitive are all
      422, and publishing a non-draft collection is 400
      (`library_collections.feature`, `steps/test_library_collections.py`)

## Known Gaps

- **Collection authoring UI is flag-gated** — create/publish authoring at
  `/library/collections/*` lives behind the `library_collection` feature
  flag; the authoring REST surface now ships an executing BDD contract
  (`library_collections.feature`), but the flag-gated frontend authoring
  forms themselves remain unit/integration-covered only (no E2E).

## QA History

- 2026-09-25: **product-map walk** — closed the "authoring not BDD-exercised"
  gap: `library_collections.feature` gained 10 authoring scenarios driving the
  real `/api/v1/libraries/collections` create / update / publish routes (patched
  flag/RLS/CRUD/pin-lookup seams) covering the draft create contract, the
  duplicate-slug 409, the draft-only mutation rule (400 for published), and the
  publish-time pin-validation error contract (empty manifest 422, duplicate pins
  422, >25 pins 422, unknown primitive 422, non-draft 400).

- 2026-09-22: **product-map walk** — closed the "No BDD feature file" gap:
  shipped `backend/tests/bdd/features/library/library_collections.feature`
  wired from `steps/test_library_collections.py`, driving the real
  `/api/v1/libraries/collections` install/uninstall/grant routes (patched
  library_service functions) — the install 201/400/422 error contract,
  uninstall delete-vs-detach semantics + 404, and the community-sourced grant
  gate (200/idempotent, 400 local, 404 unknown) with `agents_granted`.
  `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-09-10: **Branch Fixer** — registered `feat-library-collections` in the
  manifest, the graph-root registry index, and this behaviour tracker so the
  product-map architecture guards (route reference, registry index enumeration,
  behaviour tracking) stay green.
