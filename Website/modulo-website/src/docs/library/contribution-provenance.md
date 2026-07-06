---
title: Contribution Provenance
---

# Contribution Provenance

Cryptographic signing, verification, and fork tracking for community library primitives, including the Ed25519 registry protocol, publisher trust tiers, and the plugin discovery system.

- Route: `/admin/library`
- API: `GET /api/v1/registry/primitives` — browse community primitives
- API: `GET /api/v1/registry/primitives/{slug}` — get primitive detail with signature verification
- API: `POST /api/v1/registry/primitives` — publish primitive (v1 protocol)
- API: `POST /api/v1/registry/publish` — publish signed primitive (v2 protocol)
- API: `GET /api/v1/registry/pull/{slug}` — pull signed primitive (v2 protocol)
- API: `GET /api/v1/registry/verify/{slug}` — verify primitive signature
- API: `POST /api/v1/registry/publishers` — register verified publisher
- API: `GET /api/v1/plugins` — list discovered plugins
- API: `GET /api/v1/plugins/{id}/health` — plugin health check
- PRD: §8.14

See the [PRD §8.14](../../prd.md#814-community-library) for the full specification.
