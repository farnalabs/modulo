---
id: feat-pipelines-workflow-feature-files
prd: §8.15
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
depends-on: []
status: partial
---

# Shareable Workflow Bundles (Import/Export)

Bundle-based pipeline portability — export a pipeline as a `.modulo.zip` bundle, import it into another organisation with schema/connector/model backend binding.

## Behaviours

### Export

- [ ] Export pipeline as `.modulo.zip` via POST `/api/v1/libraries/export/{pipeline_id}` — response is `application/zip` with `Content-Disposition` ending in `.modulo.zip`
- [ ] Non-existent pipeline returns 404
- [ ] Exported ZIP contains `bundle.json` at archive root with `format_version`, `pipeline`, `agents`, `schemas`, `edges`
- [ ] Export strips `owner_team_id` from pipeline section
- [ ] Export strips credentials and ciphertexts from agent definitions
- [ ] Export preserves pipeline name and graph nodes
- [ ] Each agent in bundle has `name`, `prompt_template`, schema references, and `model_backend_id`
- [ ] Bundle schema uses YAML-serialisable format with `modulo_workflow` root key (`id`, `name`, `version`, `author`)
- [ ] Bundle declares `requires.connector_types` and `requires.abstract_schemas`
- [ ] Bundle embeds full schema definitions for self-containment

### Import — Analysis

- [ ] Upload `.modulo.zip` to `/api/v1/libraries/import/upload-zip` extracts bundle JSON server-side
- [ ] Analyse raw bundle JSON via `/api/v1/libraries/import/analyse`
- [ ] Analysis response contains `resolved_schemas`, `resolved_connectors`, `resolved_model_backends`, `warnings`, `name_conflicts`, `available_teams`, `bundle_json`
- [ ] Non-ZIP file upload rejected with 400
- [ ] Missing `bundle.json` in ZIP raises error
- [ ] Invalid JSON in `bundle.json` raises parse error

### Import — Schema Resolution

- [ ] Schema resolved by `abstract_name` to existing local schema (identical field structure → reuse, no import)
- [ ] Schema resolved by `definition_json` equality (structural match)
- [ ] Schema with same `abstract_name` but different field structure → imported as new schema with disambiguation suffix (e.g. `document-input-imported-1`) + warning
- [ ] Unresolved schema (not present locally) → new Schema and SchemaVersion created
- [ ] No auto-merge or silent version bump on schema conflicts — user consolidates manually

### Import — Binding

- [ ] Connector type resolved to local active instance by type match
- [ ] Unmatched connector type generates warning; creates placeholder (`status: unconfigured`) — pipeline saved but not runnable
- [ ] Schema resolved by `abstract_name` match to local schema
- [ ] Schema resolved by structure match (`definition_json` equality)
- [ ] Model backend resolved by name
- [ ] Model backend resolved by `provider`+`model_id` fallback when name does not match
- [ ] Capability check: hard block (`connector_capability_mismatch`) if bound connector instance lacks required operation
- [ ] Model backend `model_id` differs from bundle's declared `preferred_model_id` → informational warning only

### Import — Name Conflict Resolution

- [ ] Pipeline name collision: imported with "(imported)" suffix, no silent overwrite
- [ ] Agent name collision: imported with "(imported)" suffix
- [ ] Two agents in same bundle sharing identical name → rejected pre-import with validation error listing duplicates
- [ ] `suggest_import_name` appends suffix on collision, increments counter (`(imported) 2`, `(imported) 3`) on repeated collisions
- [ ] Name conflict resolution is case-sensitive — same string with different case is a distinct name

### Import — Materialization

- [ ] Import materializes real DB entities: Pipeline, Agent, Schema, SchemaVersion, PipelineEdge, LibraryPrimitive
- [ ] `agent_count` matches number of agents in bundle
- [ ] Library primitive of type `workflow` created with bundle stored in `content_json`
- [ ] Import assigns `owner_team_id` from bundle selection (ownership picker shown before confirm)
- [ ] Import defaults `visibility` to `org`
- [ ] Prompt templates displayed to user for review before confirming import
- [ ] Ed25519 signature verification on import (registry primitives); warns if unverified
- [ ] Import pipeline name can be overridden via `pipeline_name_override` in confirm request

### Edges / HITL Gates

- [ ] `edges:` block required when pipeline has HITL gates
- [ ] Bundle exported without `edges:` block silently drops all gate configuration
- [ ] Import of bundle with no `edges:` block creates linear sequential pipeline (all edges normal, no gates)
- [ ] Importer emits warning if `agents:` block implies non-linear topology not explained by `edges:` block

### Workflow Updates

- [ ] No automatic updates in v1 — manual re-import with re-binding required
- [ ] V2 registry: "check for updates" compares local checksum to registry
- [ ] Local customisations not merged automatically on re-import

### Error States

- [ ] Export non-existent pipeline returns 404
- [ ] Uploading non-ZIP file to upload-zip returns 400
- [ ] Duplicate agent names within same bundle: rejected pre-import
- [ ] Capability mismatch on connector: hard block with error
- [ ] Non-existent `owner_team_id` on import: validation error
- [ ] Missing `bundle.json` manifest in ZIP: `LookupError`

### Edge Cases

- [ ] Minimal bundle (zero agents, schemas, edges) creates just Pipeline + LibraryPrimitive
- [ ] Import with all references already resolved (schemas, connectors, model backends all match locally) — no warnings
- [ ] Import with no matching local references — all schemas created fresh, connector placeholders created
- [ ] Abstract schema namespacing: unnamespaced in local use (collision is user's responsibility); `author/name` in v2 registry
- [ ] `owner_team_id` stripped on export (org-internal reference, meaningless outside source org)
- [ ] `visibility` defaults to `org` on import regardless of source value

## Known Gaps

- No BDD scenarios for Ed25519 signature verification on import
- No BDD scenarios for prompt template review step before confirm
- No BDD scenarios for capability check hard block (connector_capability_mismatch)
- No BDD scenarios for missing edges block warning (non-linear topology implied by agents)
- No BDD scenarios for two agents with same name in bundle (pre-import rejection)
- No BDD scenarios for placeholder connector creation on unmatched type
- No BDD scenarios for model backend `model_id` mismatch informational warning
- No frontend UI for bundle upload wizard — import flow is API-only
- No BDD scenarios covering the edge/export-strip interaction for bundles with gates
- No BDD scenarios for the ownership picker UI presentation before confirm
- No BDD scenarios for workflow update / re-import flow

