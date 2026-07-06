---
title: Contribution Update
---

# Contribution Update

Submit new versions of published community contributions, list version history, and receive update notifications on forked copies.

- Route: `/admin/library`
- API: `POST /api/v1/library/contribute/{id}/versions` — submit new version
- API: `GET /api/v1/library/contribute/{id}/versions` — list all versions
- API: `POST /api/v1/library/contribute/{id}/submit` — move draft to review queue
- API: `POST /api/v1/library/contribute/{id}/publish` — publish reviewed contribution
- PRD: §8.14

See the [PRD §8.14](../../prd.md#814-contribution-versioning) for the full specification.
