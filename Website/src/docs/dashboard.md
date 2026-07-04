---
title: Dashboard
description: Org-level dashboard with run overview, team breakdown, eval quality metrics, and trend data.
---

# Dashboard

The Dashboard provides an overview of your organisation's pipelines, runs, and evaluation quality.

## Summary

- Total runs, active pipelines, and run status breakdown (running, awaiting human, failed, idle)
- Per-team metrics for team-tier organisations
- Eval pass rate with per-pipeline breakdown
- 7-day trend data (run count, eval pass rate, token spend)
- Recent runs list with status badges

## Trends

The `GET /api/v1/dashboard/trends` endpoint provides detailed time-series data including:
- Run counts, eval pass rates, and token spend by day
- HITL volume with approval/rejection rates
- Rejection trend with rolling 3-day average
- Correlation between eval pass rate and rejection rate
- Feedback volume by day

## API

- `GET /api/v1/dashboard/summary` — Org-level summary
- `GET /api/v1/dashboard/trends?days=7` — Trend data (1-90 day range)
- `GET /api/v1/dashboard/daily-run-counts?days=30` — Daily run counts by status
