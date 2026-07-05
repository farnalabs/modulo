---
title: Cryptographic Audit Chain
---

# Cryptographic Audit Chain

SHA-256 cryptographic chaining of audit events per organisation, providing tamper-evident integrity. Each event records the hash of the prior event in the org's sequence, forming a linked chain.

- Route: `/admin/audit`
- API: `GET /api/v1/admin/audit/verify` — verify integrity of the audit hash chain
- API: `GET /api/v1/admin/audit/export` — paginated export of audit events with hashes
- PRD: §8.12

See the [PRD §8.12](../../prd.md#812-audit-log) for the full specification.
