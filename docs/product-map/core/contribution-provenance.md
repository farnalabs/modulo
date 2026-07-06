---
id: feat-core-contribution-provenance
prd: 8.14
delivery-tasks:
  - task-nv8-contribution-provenance
  - task-prd-plugin-api-docs
code:
  - backend/src/modulo/core/registry/
  - backend/src/modulo/api/routes/registry.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/core/library_service/
  - backend/src/modulo/core/plugin_registry/
  - backend/src/modulo/api/routes/plugins.py
  - backend/src/modulo/db/migrations/versions/0001_initial_schema.py
  - docs/plugin-api.md
depends-on: [feat-core-contribute-primitive]
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/contribute.feature
  - backend/tests/bdd/features/library/ratings.feature
  - backend/tests/bdd/features/library/auto_update.feature
  - backend/tests/bdd/features/personas/jordan-community-contributor.feature
  - backend/tests/bdd/features/personas/alice-devx-sme.feature
  - backend/tests/bdd/features/composites/composite_library.feature
unit-tests:
  - backend/tests/unit/registry/test_crypto.py
  - backend/tests/unit/registry/test_registry.py
  - backend/tests/unit/registry/test_publisher_trust.py
  - backend/tests/unit/api/test_community_registry.py
  - backend/tests/unit/api/test_registry_publishers.py
  - backend/tests/unit/api/test_contributions.py
  - backend/tests/unit/api/test_plugin_registry_bdd.py
  - backend/tests/unit/library_service/test_library_service.py
  - backend/tests/unit/plugin_registry/test_plugin_registry.py
  - backend/tests/integration/test_initial_migration.py
  - backend/tests/unit/mcp/test_library_list_resource.py
  - backend/tests/unit/mcp/test_library_detail_resource.py
  - backend/tests/unit/mcp/test_browse_library.py
status: partial
---

# Contribution Provenance

Cryptographic signing, verification, and fork tracking for community library primitives, including the Ed25519 registry protocol, publisher trust tiers, and the plugin discovery system.

## Behaviours

### Data model & fork provenance

- [x] `library_primitives` table has `source` discriminator (`local` | `registry`)
- [x] `forked_from` is immutable after creation (enforced by DB trigger `enforce_library_fork_provenance()`)
- [x] `forked_from` must reference a `source: registry` entry (enforced by same trigger)
- [x] Registry primitives carry `checksum` (SHA-256) and `ed25519_signature`
- [x] Local primitives have `ed25519_signature` null; CHECK constraint enforces this
- [x] Registry primitives have `ed25519_signature` not null; CHECK constraint enforces this

### Registry protocol (publish / pull / verify)

- [x] Registry v1 and v2 publish endpoints accept a primitive and store it with Ed25519 signature
- [x] Publisher generates keypair via `generate_keypair()` and signs primitive with `sign_primitive()`
- [x] Registry get/download endpoints return the primitive with its `ed25519_signature_hex`
- [x] Registry pull endpoint verifies Ed25519 signature before returning the entry
- [x] Registry verify endpoint checks payload signature against a provided or built-in public key
- [x] Built-in in-memory registry (`_BUILTIN_REGISTRY`) with pre-seeded primitives
- [x] `Publisher` dataclass with trust status; `register_publisher()` / `revoke_publisher()` API
- [x] `compute_popularity_score()` and `list_registry_primitives_ranked()` for search ranking

### Signature verification & trust tiers

- [x] `verify_primitive_signature()` verifies Ed25519 signature against built-in or provided public key
- [ ] Trust tier display: **Verified publisher** (green badge) vs **Community** (amber badge)
- [ ] Verified publisher program: key issuance, application process, revocation (v2)
- [ ] Community (unsigned/self-signed) primitives show warning on copy requiring `confirm: true`
- [ ] Warning text: "This primitive has not been verified by Modulo. Review the prompt template and schema before use."

### Copy-to-adapt flow

- [x] Copy creates new row with `source: local`, `forked_from` set to registry entry ID
- [x] Local copy has `ed25519_signature` null (no signature carried forward)
- [ ] Ownership picker shown during copy: defaults to org for registry sources, same team for local sources
- [x] Community primitives are read-only via MCP (returns 403 `community_primitive_read_only`)
- [x] Browser POST to `/api/v1/libraries/{id}/adapt` succeeds (201) and creates local copy

### Rating system

- [ ] One rating per user per primitive (unique constraint)
- [x] Self-rating blocked at application layer
- [x] Rating requires at least one prior copy-to-adapt of the primitive
- [x] 10-minute submission cooldown per user
- [x] Ratings displayed as weighted average with review count
- [x] Report abuse: admin review queue

### Plugin registry (ConnectorType discovery)

- [x] `PluginRegistry.discover_plugins()` finds `modulo.connectors` and `modulo.model_backends` entry points
- [x] ConnectorType registration is in-memory at startup, not DB-backed
- [x] `ConnectorInstance.connector_type_id` is a string resolved at runtime from in-memory registry
- [x] Pre-run health check fails with `connector_type_unavailable` for missing types
- [x] Runtime `pip install` explicitly disallowed — only build-time install
- [ ] Admin UI surfaces unavailable connector types with warning badge
- [x] `GET /api/v1/plugins` returns all discovered plugins with health status
- [x] `GET /api/v1/plugins/{plugin_id}/health` returns health for a single plugin
- [x] Plugin discovery gated by `MODULO_PLUGIN_DISCOVERY` env var (default: true)
- [x] `ConnectorHub` falls back to plugin registry for unknown connector types
- [x] `ModelBackendHub` falls back to plugin registry for unknown model providers
- [x] Entry point load failures are recorded as unhealthy (not silently dropped)
- [x] `get_plugin_registry()` returns a module-level singleton shared across consumers
- [x] Manual `register_connector_type()` / `register_model_backend()` available for in-tree registration
- [x] Plugin API documented at `docs/plugin-api.md`

### Error handling

- [x] `verify_manifest()` returns `False` (not raises) on `InvalidSignature` — verified in `core/registry/__init__.py:149-152`
- [x] `verify_bundle_integrity()` comparison handles empty/mismatched strings — verified at `core/registry/__init__.py:167-169`
- [x] `revoke_publisher()` returns `False` on missing fingerprint (not raises) — verified at `core/registry/__init__.py:571-574`
- [x] `get_publisher_status()` returns `"community"` for unknown fingerprints (not raises) — verified at `core/registry/__init__.py:586-588`
- [x] `CommunityPrimitiveReadOnlyError` raised for MCP adapt of community primitives → 403 — verified in `library_service/__init__.py:1178-1179` + `routes/library.py:500-504`
- [x] `ContributionInvalidTransitionError` for bad contribution status transitions — verified in `routes/contributions.py:117-118` → 409
- [x] `ContributionNotFoundError` for missing primitives → 404 — verified in `routes/contributions.py:115-116`
- [x] `ProgrammingError` caught in `get_primitive()` / `get_primitive_by_slug()` → returns `None` gracefully — verified in `library_service/__init__.py:1108-1113` and `:1148-1153`
- [x] Registry 404 for missing slug in get/pull/verify endpoints — all four endpoints confirmed
- [x] Registry 403 for signature verification failure in v2 publish — `routes/registry.py:386-390`
- [x] Registry 400 for invalid Ed25519 PEM format in v2 publish — `routes/registry.py:397-402`
- [x] Registry 404 for publisher not found on revoke — `routes/registry.py:591-593`
- [x] Registry 404 for missing primitive in download endpoint — `routes/registry.py:249-253`
- [x] Registry endpoints that are in-memory-only do not need `ProgrammingError` catches (correct, no DB dependency)

### BDD-tested scenarios

- [x] Browse: list all primitives, filter by type, search by name, filter to local only, view single primitive
- [x] Copy-to-adapt: browser adapt succeeds, MCP adapt returns 403, team assignment, non-existent returns 404
- [x] Ratings: view aggregate, submit thumbs-up/down, list ratings, aggregate updates after new rating

## Known Gaps

### Registry & signing
- BDD feature files exist on disk (`tests/features/library/community_registry.feature`, `tests/features/plugins/plugin_registry.feature`), step definitions exist and reference correct paths — BUT `plugin_registry.feature` has `@awaiting-implementation` tags on most scenarios (discovery, detail, startup). `signing.feature` covers webhook HMAC signing only — there is no dedicated Ed25519 registry signing BDD feature.
- No frontend trust tier display (green/amber badges) or community warning flow — confirmed missing; only `communityPrimitives` computed exists in `LibraryView.vue` without UI for it
- No frontend ownership picker for copy-to-adapt
- No unit tests for `forked_from` constraints at the service layer (only integration test in `test_initial_migration.py` covers DB-level trigger; BDD steps exist for forked_from assertions)
- No API-layer Ed25519 verification integration test for downloaded primitives (unit tests use mock registry only)
- Verified publisher program is v2 roadmap — not yet implemented

### Plugin registry
- No BDD tests for plugin registry discovery or health check
- No BDD tests for connector type unavailability on missing plugin
- No BDD tests for plugin REST API (`GET /api/v1/plugins`)
- Admin UI warning badge for unavailable connector types not yet implemented
- `@awaiting-implementation` scenarios in `plugin_registry.feature` are not runnable

### Ratings
- No BDD tests for rating submission cooldown enforcement
- No BDD tests for self-rating block
- No BDD tests for rating-requires-prior-adapt rule
- No BDD tests for ownership picker during copy-to-adapt