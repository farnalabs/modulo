---
title: Community Library
---

# Community Library

A separate "Community" tab in the Library UI, distinct from the
Modulo-maintained Native library, showing opinionated example pipelines
contributed under `source="community"`. Community items are never mixed
into the Native list and are labeled "not verified" everywhere they appear.

- Route: `/admin/library`
- API: `GET /api/v1/libraries?source=community` — list community primitives
- API: `GET /api/v1/libraries` (default) — merges community items with local and native
- API: `POST /api/v1/libraries/{id}/adapt` — copy a community primitive to your org (browser UI only)
- PRD: §15

See the [PRD §15](../../prd.md#15-community-library) for the full specification.
