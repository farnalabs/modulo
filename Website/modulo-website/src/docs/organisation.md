---
title: Organisation
---

# Organisation

Manage your organisation — the root tenant entity in Modulo's multi-tenant architecture.

- API: `GET /api/v1/admin/org` — view org profile
- API: `PUT /api/v1/admin/org` — update org name/logo
- API: `POST /api/v1/admin/org/regenerate-api-key` — rotate default API key
- API: `POST /api/v1/admin/org/deletion-request` — initiate soft-delete
- API: `POST /api/v1/admin/org/deletion-confirm` — confirm with token
- API: `PATCH /api/v1/admin/org/deletion-cancel` — cancel pending deletion
- API: `GET /api/v1/admin/org/export` — export org data bundle
- API: `DELETE /api/v1/admin/org` — immediate hard delete

See the [PRD §9.1](../../prd.md#91-organisation-lifecycle) and [PRD §6.2](../../prd.md#62-modulo-cloud) for the full specification.
