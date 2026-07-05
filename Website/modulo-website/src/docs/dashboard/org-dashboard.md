---
title: Org Dashboard
---

# Org Dashboard

Org-level dashboard with run overview, team breakdown, eval quality metrics, trend data, HITL analytics, and feedback volume.

- Route: `/`
- API: `GET /api/v1/dashboard/summary` — org summary with counts, team breakdown, eval pass rate, 7-day trend
- API: `GET /api/v1/dashboard/trends` — configurable range (1–90 day) trend data including HITL volume, rejection rates, feedback volume, and correlation
- PRD: §14

See the [PRD §14](../../prd.md#14-dashboard) for the full specification.
