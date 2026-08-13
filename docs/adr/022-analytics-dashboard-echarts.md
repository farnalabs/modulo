# ADR 022 — Analytics dashboards: ECharts adopted; ADR 020 decisions carried forward (FAR-93 / FAR-96)

**Date:** 2026-08-12
**Status:** Accepted

---

## Context

ADR 020 (FAR-91) established the analytics foundation: the `run_daily_facts`
table, the typed-params query surface, rolling-window semantics, facts
retention, and the reconcile/drop go/no-go. At the time it explicitly noted
**"No charting dependency is added in this PR"** and recorded ECharts as the
*intended* charting choice for later.

Since then the analytics frontend shipped (FAR-93, PR #747): `echarts`
(^6.1.0) and `vue-echarts` (^8.0.1) are real dependencies in
`frontend/package.json`, and `frontend/src/components/analytics/AnalyticsChart.vue`
lazy-loads both. The "no charting dependency" note in ADR 020 is therefore
outdated and is superseded here.

FAR-96 is the documentation pass that verifies the analytics/retention docs
match the shipped behaviour. The remaining decisions this ticket asked to
record were already recorded in ADR 020; this ADR carries them forward by
reference rather than duplicating them (ADR 020 remains the canonical record).

## Decision 1 — ECharts is adopted as the analytics/dashboard charting library

The frontend renders analytics series with **ECharts via `vue-echarts`**
(lazy-loaded with a dynamic import, so the chart library is not in the initial
bundle). This **supersedes the "No charting dependency is added in this PR"**
note in ADR 020 Decision 3 and confirms the choice ADR 020 recorded as planned:

- ECharts was chosen over hand-rolled SVG/canvas (the rolling-window dashboards
  need time-series line/area charts, tooltips, and period arrows out of the
  box) and over D3 (D3's composability comes at a steep maintenance cost for
  charting that is 90% "series over time").
- The client renders only — the backend is the sole bucketing authority
  (ADR 020 Decision 3); `buildChartOption` is a pure series → ECharts-option
  mapping. Pre-coverage buckets render as gaps (`null`), never zero-filled.

## Decision 2 — Decisions carried forward from ADR 020 (no re-record)

The following analytics decisions are already canonically recorded in
`docs/adr/020-analytics.md` and are unchanged. This ADR does not restate them:

| Topic | Canonical record |
|---|---|
| Facts-vs-live aggregation (why a facts table, not live `runs` queries) | ADR 020 Decision 1 |
| `run_id` deliberately NOT a FK (facts survive the 90-day run purge) | ADR 020 Decision 2 |
| No query language — structured typed params are the surface; syntax mode deferred until a pre-rolled dependency slots in | ADR 020 Decision 3 |
| Rolling-window rationale (industry convention, timezone-agnostic, eliminates partial-period bias) | ADR 020 Decision 4 |
| 13-month facts retention config (`analytics_facts_retention_months`; 25 months as a future YoY option) | ADR 020 Decision 6 |
| Go/no-go trigger for dropping facts if >90d dimensioned history is never requested | ADR 020 Decision 6 |
| Escalation triggers for composite indexes (P95 over budget → add matching composite index via a normal migration) | ADR 020 Decision 7 |

## Consequences

- `echarts` + `vue-echarts` are committed dependencies; any future
  hand-rolled-SVG alternative must be justified against this decision.
- Docs that referenced "no charting dependency" are corrected (PRD §8.32.8 and
  this ADR); ADR 020's ECharts-context note is read as superseded in its
  charting-dependency aspect only.
- The facts/retention/reconcile decisions remain exactly as ADR 020 recorded
  them — one canonical record, no drift between ADRs.
