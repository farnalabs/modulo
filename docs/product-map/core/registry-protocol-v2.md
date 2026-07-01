---
id: feat-core-registry-protocol-v2
prd: 8.14
delivery-tasks: [task-nv8-registry-protocol-v2]
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/ratings.feature
code:
  - backend/src/modulo/api/routes/registry.py
  - backend/src/modulo/core/registry/__init__.py
  - backend/src/modulo/core/registry/crypto.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/db/crud/publisher.py
unit-tests:
  - backend/tests/unit/registry/test_crypto.py
  - backend/tests/unit/registry/test_registry.py
  - backend/tests/unit/registry/test_publisher_trust.py
  - backend/tests/unit/api/test_registry_publishers.py
depends-on: [feat-core-contribute-primitive]
status: partial
---

# Registry Protocol v2

Ed25519-signed publish/pull/verify protocol for community primitives. Supports 6 primitive types, trust tiers, bundle integrity checking, copy-to-adapt workflow, and in-memory built-in registry with 9 seeded entries (3 original + 6 dogfood). Author/name namespaced slugs.

## Behaviours

### V2 Publish Protocol

- [ ] POST /api/v1/registry/publish accepts PublishRequestV2 (6 primitive types: schema, workflow, agent, integration, test_fixture, pipeline_template)
- [ ] Generates temp Ed25519 keypair, signs canonical JSON, stores entry
- [ ] Computes SHA-256 checksum of canonical bundle JSON
- [ ] Returns PublishResponseV2 with slug, version, checksum, signature, fingerprint, verified
- [ ] Duplicate slug overwrites existing entry (in-memory registry)
- [ ] Missing/invalid signing_key_hex raises error
- [ ] Returns 201 on success

### V2 Pull Protocol

- [ ] GET /api/v1/registry/pull/{slug} returns PullResponseV2 with full metadata + content
- [ ] Returns 404 when slug does not exist
- [ ] Verifies Ed25519 signature before returning
- [ ] Includes publisher_status (verified/community/revoked) from fingerprint lookup
- [ ] slug is author/name path — contains embedded slash

### V2 Verify Protocol

- [ ] GET /api/v1/registry/verify/{slug} returns VerifyResponseV2 with verified boolean
- [ ] Accepts optional public_key_hex query parameter for key-specific verification
- [ ] With public_key_hex: verifies against provided key, looks up publisher trust_tier and publisher_name from DB
- [ ] Without public_key_hex: uses built-in registry development key (verify_primitive_signature)
- [ ] Unknown slug returns 404
- [ ] Returns trust_tier=None, publisher_name=None when no DB match found
- [ ] Returns publisher_status (verified/community/revoked) regardless of verification method

### V1 Endpoints (legacy compatibility)

- [ ] GET /api/v1/registry/primitives — list ranked with publisher trust badges and popularity score
- [ ] GET /api/v1/registry/primitives/{slug} — single entry with signature + integrity verification
- [ ] POST /api/v1/registry/primitives — publish with signing_key_hex (accepts 4 primitive types)
- [ ] POST /api/v1/registry/primitives/{slug}/download — download, increment count, create local LibraryPrimitive copy
- [ ] All v1 endpoints support filters: author, primitive_type, search, sort_by (popularity/recent/downloads/rating)
- [ ] Pagination with page/page_size (default 20, max 100)

### Ed25519 Signing & Crypto

- [ ] generate_keypair() returns hex-encoded private_key, public_key, and 16-char fingerprint
- [ ] sign_primitive() signs canonical JSON (sort_keys=True, separators=",:") with Ed25519 private key
- [ ] verify_signature() verifies Ed25519 signature against canonical JSON
- [ ] Tampered payload → verification returns False
- [ ] Wrong signing key → verification returns False
- [ ] Tampered signature hex → verification returns False
- [ ] SHA-256 bundle integrity: compute_bundle_hash / verify_bundle_integrity
- [ ] Canonical JSON is deterministic — same inputs produce same bytes
- [ ] Fingerprint = SHA-256(public_key_raw)[:16] — stable hex identifier

### Publisher Trust Model

- [ ] Three statuses: verified, community, revoked
- [ ] Built-in modulo publisher pre-registered as verified
- [ ] register_publisher() creates a verified publisher by fingerprint
- [ ] revoke_publisher() sets status to revoked; returns False for unknown fingerprint
- [ ] get_publisher_status() returns verified/community/revoked; unknown fingerprint → community
- [ ] list_verified_publishers() returns only verified-status publishers
- [ ] Popularity score computed from downloads (40%), rating (40%), recency (20%) + review bonus

### In-Memory Built-in Registry

- [ ] 9 seeded entries: 3 original (PRD schema, requirements schema, PRD workflow) + 6 dogfood entries
- [ ] All built-in primitives have valid Ed25519 signatures
- [ ] All built-in primitives have 64-char SHA-256 checksums
- [ ] Dogfood pipeline includes HITL gate config (review_before_pr)
- [ ] list_registry_primitives() filters by author, primitive_type, search term
- [ ] get_registry_primitive(slug) returns entry or None
- [ ] publish_primitive() adds entry to in-memory dict
- [ ] resolve_namespaced_slug splits author/name; defaults author to "modulo"

### Copy-to-Adapt (Library Primitive Creation)

- [ ] Download endpoint creates local LibraryPrimitive with source="registry", forked_from set
- [ ] Slug sanitised: slash replaced with dash for local rows
- [ ] Download increments entry.download_count in registry
- [ ] Adapt via browser (POST /api/v1/libraries/{id}/adapt) returns 201 with new local primitive
- [ ] Adapt via MCP for community primitives returns 403 with "community_primitive_read_only"
- [ ] Adapt with target_team_id sets owner_team_id on new primitive
- [ ] Adapt of non-existent primitive returns 404
- [ ] Library primitives have owner_team_id (nullable) and visibility (org/team)
- [ ] Community registry entries always visibility: org

### Rating System

- [ ] View rating aggregate: average_rating, review_count
- [ ] Submit thumbs-up rating with optional comment (POST 201)
- [ ] Submit thumbs-down rating without comment
- [ ] List ratings for a primitive with id, thumbs_up, comment, created_at
- [ ] Aggregate updates after new rating submitted

### Edge Cases

- [ ] Unknown slug in any endpoint → 404
- [ ] Concurrent publish of same slug → last write wins (in-memory, no locking)
- [ ] Signature verification with unknown/unregistered publishing key → returns False
- [ ] Bundle integrity mismatch → integrity_ok=False, download still permitted
- [ ] Missing signing_key_hex on publish → ValueError from from_private_bytes
- [ ] Invalid hex encoding in signing_key_hex → ValueError on bytes.fromhex
- [ ] publisher DB lookup with no DB match → trust_tier and publisher_name are None
- [ ] resolve_namespaced_slug with no slash → defaults to "modulo" author

### Error Handling

- [ ] 404 on GET/POST to non-existent slug (pull, verify, download)
- [ ] 404 on publisher revoke of unknown fingerprint
- [ ] 403 on MCP copy of community primitive
- [ ] Popularity score handles missing rating (None → 0.0)
- [ ] Verify endpoint with non-existent primitive returns 404 before any crypto

### Security

- [ ] All endpoints behind AuthenticatedPrincipal dependency
- [ ] RLS scoped to caller's org for download (creates LibraryPrimitive in org)
- [ ] Ed25519 signatures prevent tampering of published primitives
- [ ] SHA-256 checksums prevent bundle corruption
- [ ] Publisher trust tiers (verified/community) visible to end users
- [ ] Private key never exposed in response payloads
- [ ] canonical JSON prevents signature ambiguity from key ordering
- [ ] In-memory registry is development-only — no production persistence

## Known Gaps
- No hosted/remote registry — in-memory only (production would POST to modulo-operated API)
- No version pinning beyond "1.0" — all entries version-stamped "1.0"
- No version upgrade path for registry entries
- No deletion of published primitives
- No application-layer rate limiting on publish/download endpoints
- No self-rating block on ratings endpoint (PRD 8.14 spec)
- No 10-minute rating cooldown enforcement (PRD 8.14 spec)
- No "must have adapted before rating" check (PRD 8.14 spec)
- No abuse report queue for ratings
- No verified publisher application workflow (PRD: v2 roadmap)
- No publisher key rotation mechanism
- Popularity score rating factor always uses 0.0 (no rating data fed through)
- No library contribution (v2) — evals contributed back to community not implemented
- No integration/plugin install via UI — build-time only 