---
id: feat-core-hitl-effort-trend
prd: 
delivery-tasks: [task-nv7-hitl-effort-trend]
bdd:
  - backend/tests/bdd/features/observability/metrics.feature (placeholder)
  - backend/tests/bdd/features/ui/eval_dashboard.feature (placeholder)
code:
  - backend/src/modulo/api/routes/dashboard.py
  - backend/tests/unit/api/test_dashboard.py
  - docs/grafana/hitl-review.json
  - docs/grafana/README.md
depends-on: [task-nv2-eval-engine, task-nv4-feedback-record]
status: partial
---

# HITL Effort Trend

HITL decision volume, rejection rates, review-time metrics, and trend visualisation over configurable date ranges. Backend `GET /api/v1/dashboard/trends` endpoint and Grafana dashboard are delivered; no frontend trend UI or BDD coverage exists.

## Behaviours

### API — HITL Volume (`GET /api/v1/dashboard/trends`)

- [x] Returns `hitl_volume` array with per-day entries aligned to requested `days` range
- [x] Each hitl_volume entry contains `total_decisions`, `approved_count`, `rejected_count`, `rejection_rate`, `avg_time_to_approve_ms`
- [x] Returns `rejection_trend` array with `rolling_rejection_rate` (3-day average) and `raw_rejection_rate` per day
- [x] Returns `correlation` array with `rejection_rate` vs `eval_pass_rate` per day
- [x] Returns `feedback_volume` array with `feedback_count`, `resolved_count`, `correcting_count` per day
- [x] All trend series have identical length matching requested `days`
- [x] Accepts `days` parameter (default 7); works with 30 and 90
- [x] Rejects `days=0` and `days=91`
- [x] Requires authentication
- [ ] BDD feature for HITL effort trends (metrics.feature is placeholder)
- [ ] BDD scenario: dashboard shows HITL volume over time
- [ ] BDD scenario: rejection trend is computed and visible

### API — Shape and edge cases

- [x] Empty period returns zero-filled arrays with correct length
- [x] Day with no decisions shows `total_decisions=0`, `rejection_rate=0.0`, `avg_time_to_approve_ms=None`
- [x] Rolling rejection rate handles partial window (first 2 days use available data)
- [x] Correlation entries pair null eval_pass_rate with available rejection_rate when eval data is missing

### Grafana Dashboard

- [x] `hitl-review.json` dashboard imports into Grafana
- [x] Panel: gates per day (`modulo_hitl_gates_total` counter by `hitl_status`)
- [x] Panel: average review time (`modulo_hitl_review_time_seconds`)
- [x] Panel: approval rate (`approved / total * 100`)
- [x] Panel: pending gates gauge (`modulo_hitl_gates_active`)
- [x] Panel: claim token expiry (`modulo_hitl_claim_tokens_total` by `status`)
- [x] Filterable by pipeline_name via dashboard variable
- [x] Dashboard variables: datasource, pipeline_name

### Frontend

- [ ] HITL volume / rejection trend chart visualisation on dashboard page
- [ ] Trends page consuming `GET /api/v1/dashboard/trends`
- [ ] HITL volume card showing total decisions, approval rate, avg review time
- [ ] Rejection trend line chart with 3-day rolling average overlay

### Unit tests

- [x] test_hitl_volume_present — hitl_volume and rejection_trend keys exist
- [x] test_hitl_volume_structure — per-entry shape validated
- [x] test_rejection_trend_structure — per-entry shape validated
- [x] test_correlation_structure — per-entry shape validated
- [x] test_feedback_volume_structure — per-entry shape validated
- [x] test_all_trends_align_by_day_count — all series same length

## Known Gaps

- No explicit PRD section reference — feature is part of nv7 batch, aligned with Grafana HITL dashboard (§14 V1 Core — observability UI)
- No BDD feature for HITL effort trends (`metrics.feature` is a placeholder; `eval_dashboard.feature` is a placeholder)
- No frontend HITL trend visualisation — API endpoint is fully implemented but has no consuming UI
- Grafana dashboard requires manual import (not provisioned as code)
- No per-team HITL effort breakdown (only org-level in trends endpoint)
- No HITL effort export (CSV, chart image)
- No automated alert on HITL volume spikes or rejection rate thresholds
