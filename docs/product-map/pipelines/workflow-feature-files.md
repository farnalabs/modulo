---
id: feat-pipelines-workflow-feature-files
prd: 8.15
delivery-tasks: [task-nv12-workflow-feature-files]
bdd:
  - backend/tests/bdd/features/workflows/import.feature
  - backend/tests/bdd/features/workflows/export.feature
  - backend/tests/bdd/features/workflows/binding.feature
code:
  - backend/src/modulo/core/workflow_import_export/__init__.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/core/registry/__init__.py
  - frontend/src/views/LibraryView.vue
  - frontend/src/views/LibraryPipelineWizard.vue
unit-tests:
  - backend/tests/unit/library_service/test_workflow_import_export.py
  - backend/tests/unit/library_service/test_workflow_import_export_resilience.py
depends-on: [feat-pipelines-core, feat-pipelines-library]
status: partial
---

# Shareable Workflow Bundles (Import/Export)

Bundle-based pipeline portability — export a pipeline as a `.modulo.zip` bundle,
import it into another organisation with schema/connector/model backend binding.

## Behaviours

### Export

- [x] Export pipeline as `.modulo.zip` via POST `/api/v1/libraries/export/{pipeline_id}`
  — response is `application/zip` with `Content-Disposition` ending in `.modulo.zip`
- [x] Non-existent pipeline returns 404
- [x] Exported ZIP contains `bundle.json` at archive root with
  `format_version`, `pipeline`, `agents`, `schemas`, `edges`
- [x] Export strips `owner_team_id` from pipeline section
- [x] Export strips credentials and ciphertexts from agent definitions
- [x] Export preserves pipeline name and graph nodes
- [x] Each agent in bundle has `name`, `prompt_template`, schema references, and `model_backend_id`
- [x] Bundle schema uses JSON format with `format_version`, `pipeline`, `agents`, `schemas`, `edges` keys (not YAML as in PRD)
- [ ] Bundle declares `requires.connector_types` and `requires.abstract_schemas`
- [x] Bundle embeds full schema definitions for self-containment
- [x] Export preserves `hitl_gate_config` on edges (fixed in cross-cutting QA — was silently dropping all gate config)

### Import — Analysis

- [x] Upload `.modulo.zip` to `/api/v1/libraries/import/upload-zip` extracts bundle JSON server-side
- [x] Analyse raw bundle JSON via `/api/v1/libraries/import/analyse`
- [x] Analysis response contains `resolved_schemas`, `resolved_connectors`,
  `resolved_model_backends`, `warnings`, `name_conflicts`, `available_teams`,
  `bundle_json`
- [x] Non-ZIP file upload rejected with 400
- [x] Missing `bundle.json` in ZIP raises error
- [x] Invalid JSON in `bundle.json` raises parse error

### Import — Schema Resolution

- [x] Schema resolved by `abstract_name` to existing local schema (identical field structure → reuse, no import)
- [x] Schema resolved by `definition_json` equality (structural match)
- [x] Schema with same `abstract_name` but different field structure → imported as
  new schema with disambiguation suffix (e.g. `document-input-imported-1`) + warning
- [x] Unresolved schema (not present locally) → new Schema and SchemaVersion created
- [x] No auto-merge or silent version bump on schema conflicts — user consolidates manually

### Import — Binding

- [x] Connector type resolved to local active instance by type match
- [x] Unmatched connector type generates warning; creates placeholder
  (`status: unconfigured`) — pipeline saved but not runnable
- [x] Schema resolved by `abstract_name` match to local schema
- [x] Schema resolved by structure match (`definition_json` equality)
- [x] Model backend resolved by name
- [x] Model backend resolved by `provider`+`model_id` fallback when name does not match
- [x] Capability check: hard block (`connector_capability_mismatch`) if bound
  connector instance lacks required operation
- [x] Model backend `model_id` differs from bundle's declared `preferred_model_id` — informational warning only

### Import — Name Conflict Resolution

- [x] Pipeline name collision: imported with "(imported)" suffix, no silent overwrite
- [x] Agent name collision: imported with "(imported)" suffix
- [x] Two agents in same bundle sharing identical name — warning emitted during analysis (added in cross-cutting QA)
- [x] `suggest_import_name` appends suffix on collision, increments counter
  (`(imported) 2`, `(imported) 3`) on repeated collisions
- [x] Name conflict resolution is case-sensitive — same string with different case is a distinct name

### Import — Materialization

- [x] Import materializes real DB entities: Pipeline, Agent, Schema, SchemaVersion, PipelineEdge, LibraryPrimitive
- [x] `agent_count` matches number of agents in bundle
- [x] Library primitive of type `workflow` created with bundle stored in `content_json`
- [x] Import assigns `owner_team_id` from bundle selection (ownership picker shown before confirm)
- [x] Import defaults `visibility` to `org`
- [ ] Prompt templates displayed to user for review before confirming import
- [x] Ed25519 signature verification on import (registry primitives); warns if unverified
- [x] Import pipeline name can be overridden via `pipeline_name_override` in confirm request

### Edges / HITL Gates

- [x] `edges:` block required when pipeline has HITL gates
- [x] Bundle exported without `edges:` block silently drops all gate configuration
- [x] Import of bundle with no `edges:` block creates linear sequential pipeline (all edges normal, no gates)
- [x] Importer emits warning if `agents:` block implies non-linear topology not explained by `edges:` block

### Workflow Updates

- [x] No automatic updates in v1 — manual re-import with re-binding required
- [ ] V2 registry: "check for updates" compares local checksum to registry
- [x] Local customisations not merged automatically on re-import

### Error States

- [x] Export non-existent pipeline returns 404
- [x] Uploading non-ZIP file to upload-zip returns 400
- [x] Duplicate agent names within same bundle: rejected pre-import
- [x] Capability mismatch on connector: hard block with error
- [x] Non-existent `owner_team_id` on import: validation error
- [x] Missing `bundle.json` manifest in ZIP: `LookupError`

### Edge Cases

- [x] Minimal bundle (zero agents, schemas, edges) creates just Pipeline + LibraryPrimitive
- [x] Import with all references already resolved (schemas, connectors, model backends
  all match locally) — no warnings
- [x] Import with no matching local references — all schemas created fresh, connector placeholders created
- [x] Abstract schema namespacing: unnamespaced in local use (collision is user's
  responsibility); `author/name` in v2 registry
- [x] `owner_team_id` stripped on export (org-internal reference, meaningless outside source org)
- [x] `visibility` defaults to `org` on import regardless of source value (hardcoded in materialize_import, line 671)

### Error Handling (added in cross-cutting QA)

- [x] confirm_import endpoint catches ProgrammingError → 501
- [x] confirm_import endpoint catches SQLAlchemyError → 503
- [x] _analyse_bundle catches ProgrammingError → 501
- [x] _analyse_bundle catches SQLAlchemyError → 503
- [x] materialize_import validates owner_team_id exists before creating entities
- [x] export_pipeline uses single transaction for pipeline lookup and bundle building
- [x] _get_latest_published_version catches SQLAlchemyError and logs warning
- [x] export_pipeline_bundle catches SQLAlchemyError and logs warning
- [x] Non-existent owner_team_id on import: validation error

### Additional Edge Cases (added in cross-cutting QA)

- [x] Bundle format version mismatch raises ValueError at materialize step
- [x] Oversized bundle (>100MB in core function, >50MB at API boundary) rejected
- [x] Edge with missing source/target node IDs raises ValueError → 500
- [x] Concurrency: non-existent owner_team_id produces validation error, not cryptic FK violation
- [ ] Import with already-deleted team produces clear validation error
- [x] `hitl_gate_config` now preserved in edge export (fixed in cross-cutting QA)
- [x] Duplicate agent name within bundle detected as warning during analysis (added in cross-cutting QA)

## QA History

- 2026-07-05: Prodmap pipelines QA: Added depends-on (feat-pipelines-core, feat-pipelines-library). Added QA History section. Moved `confirm_import` account_id bug fix description from Known Gaps to QA History.
- 2026-07-06: Cross-cutting QA on feat-pipelines-workflow-feature-files:
  - Verified all export behaviours match code
  - Verified all import analysis/resolution/binding behaviours match code
  - Verified all ProgrammingError catches on library.py routes
  - Fixed: `hitl_gate_config` was silently dropped during edge export — now exported
  - Added: duplicate agent name within bundle warning in `_analyse_bundle`
  - Verified: `visibility` hardcoded to `org` in `materialize_import`
  - Known gaps confirmed: `requires.connector_types`/`requires.abstract_schemas` not implemented,
    capability check not implemented, no prompt template review UI

## Known Gaps

- Bundle schema uses JSON format with `format_version`, `pipeline`, `agents`, `schemas`, `edges` keys — diverges from PRD's YAML-serialisable `modulo_workflow` root key spec
- Missing `requires.connector_types` and `requires.abstract_schemas` top-level section in bundle — not implemented
- Ed25519 verification on import is not wired to real Ed25519 checks in practice (registry primitives exist but no actual crypto verification)
- No prompt template review UI step before confirm — import flow skips user review of agent prompts
- No dedicated ownership picker endpoint — ownership is assigned via bundle selection in the confirm request only
- No frontend UI for bundle upload wizard — import flow is API-only
- No BDD scenarios for Ed25519 signature verification on import
- No BDD scenarios for prompt template review step before confirm
- No BDD scenarios for capability check hard block (connector_capability_mismatch)
- No BDD scenarios for missing edges block warning (non-linear topology implied by agents)
- No BDD scenarios for placeholder connector creation on unmatched type
- No BDD scenarios for model backend `model_id` mismatch informational warning
- No BDD scenarios covering the edge/export-strip interaction for bundles with gates
- No BDD scenarios for the ownership picker UI presentation before confirm
- No BDD scenarios for workflow update / re-import flow
- `visibility` defaults to `org` on import regardless of source value (verified — hardcoded in materialize_import)
- No BDD scenarios for the import confirm endpoint covering error paths
- No capability/connector_capability check on connector binding during import analysis — binding warns on unmatched type but does not check that the matched instance supports required operations
- No BDD scenarios for the duplicate agent name warning within bundle
