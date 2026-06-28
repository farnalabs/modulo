---
id: feat-core-contribution-provenance
prd: §8.14
delivery-tasks: [task-nv8-contribution-provenance]
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/ratings.feature
code:
  - backend/src/modulo/core/registry/
  - backend/src/modulo/api/routes/registry.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/core/library_service/
  - backend/src/modulo/core/plugin_registry/
  - backend/src/modulo/db/migrations/versions/0001_initial_schema.py
depends-on: [task-nv8-contribute-primitive]
status: partial
---

# Contribution Provenance

Cryptographic signing, verification, and fork tracking for community library primitives, including the Ed25519 registry protocol, publisher trust tiers, and the plugin discovery system.

## Behaviours

### Data model & fork provenance
- [ ] `library_primitives` table has `source` discriminator (`local` | `registry`)
- [ ] `forked_from` is immutable after creation (enforced by DB trigger `enforce_library_fork_provenance()`)
- [ ] `forked_from` must reference a `source: registry` entry (enforced by same trigger)
- [ ] Registry primitives carry `checksum` (SHA-256) and `ed25519_signature`
- [ ] Local primitives have `ed25519_signature` null; CHECK constraint enforces this
- [ ] Registry primitives have `ed25519_signature` not null; CHECK constraint enforces this

### Registry protocol (publish / pull / verify)
- [ ] Registry v1 and v2 publish endpoints accept a primitive and store it with Ed25519 signature
- [ ] Publisher generates keypair via `generate_keypair()` and signs primitive with `sign_primitive()`
- [ ] Registry get/download endpoints return the primitive with its `ed25519_signature_hex`
- [ ] Registry pull endpoint verifies Ed25519 signature before returning the entry
- [ ] Registry verify endpoint checks payload signature against a provided or built-in public key
- [ ] Built-in in-memory registry (`_BUILTIN_REGISTRY`) with pre-seeded primitives
- [ ] `Publisher` dataclass with trust status; `register_publisher()` / `revoke_publisher()` API
- [ ] `compute_popularity_score()` and `list_registry_primitives_ranked()` for search ranking

### Signature verification & trust tiers
- [ ] `verify_primitive_signature()` verifies Ed25519 signature against built-in or provided public key
- [ ] Trust tier display: **Verified publisher** (green badge) vs **Community** (amber badge)
- [ ] Verified publisher program: key issuance, application process, revocation (v2)
- [ ] Community (unsigned/self-signed) primitives show warning on copy requiring `confirm: true`
- [ ] Warning text: "This primitive has not been verified by Modulo. Review the prompt template and schema before use."

### Copy-to-adapt flow
- [ ] Copy creates new row with `source: local`, `forked_from` set to registry entry ID
- [ ] Local copy has `ed25519_signature` null (no signature carried forward)
- [ ] Ownership picker shown during copy: defaults to org for registry sources, same team for local sources
- [ ] Community primitives are read-only via MCP (returns 403 `community_primitive_read_only`)
- [ ] Browser POST to `/api/v1/libraries/{id}/adapt` succeeds (201) and creates local copy

### Rating system
- [ ] One rating per user per primitive (unique constraint)
- [ ] Self-rating blocked at application layer
- [ ] Rating requires at least one prior copy-to-adapt of the primitive
- [ ] 10-minute submission cooldown per user
- [ ] Ratings displayed as weighted average with review count
- [ ] Report abuse: admin review queue

### Plugin registry (ConnectorType discovery)
- [ ] `PluginRegistry.discover_plugins()` finds `modulo.connectors` and `modulo.model_backends` entry points
- [ ] ConnectorType registration is in-memory at startup, not DB-backed
- [ ] `ConnectorInstance.connector_type_id` is a string resolved at runtime from in-memory registry
- [ ] Pre-run health check fails with `connector_type_unavailable` for missing types
- [ ] Runtime `pip install` explicitly disallowed — only build-time install
- [ ] Admin UI surfaces unavailable connector types with warning badge

### BDD-tested scenarios
- [ ] Browse: list all primitives, filter by type, search by name, filter to local only, view single primitive
- [ ] Copy-to-adapt: browser adapt succeeds, MCP adapt returns 403, team assignment, non-existent returns 404
- [ ] Ratings: view aggregate, submit thumbs-up/down, list ratings, aggregate updates after new rating

## Known Gaps

- No BDD tests for registry publish or pull endpoints
- No BDD tests for Ed25519 signature verification
- No BDD tests for fork provenance immutability (DB trigger)
- No BDD tests for trust tier display or community warning flow
- No BDD tests for publisher registration or revocation
- No BDD tests for plugin registry discovery or health check
- No BDD tests for rating submission cooldown enforcement
- No BDD tests for self-rating block
- No BDD tests for rating-requires-prior-adapt rule
- No BDD tests for ownership picker during copy-to-adapt
- No BDD tests for connector type unavailability on missing plugin

