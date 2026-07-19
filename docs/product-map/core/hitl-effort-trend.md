---
id: feat-core-hitl-effort-trend
prd: 8.8
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/dashboard/hitl_trends.feature
code:
  - backend/src/modulo/api/routes/dashboard.py
  - backend/tests/unit/api/test_dashboard.py
  - docs/grafana/hitl-review.json
  - docs/grafana/README.md
depends-on: [feat-evals-eval-engine, feat-evals-feedback-records]
unit-tests:
  - backend/tests/unit/api/test_dashboard.py
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
- [x] BDD feature for HITL effort trends (hitl_trends.feature)
- [x] BDD scenario: dashboard shows HITL volume over time (hitl_trends.feature)
- [x] BDD scenario: rejection trend is computed and visible (hitl_trends.feature)

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

### Error Handling

- [x] `dashboard_summary` catches `ProgrammingError` → 501, `SQLAlchemyError` → 500, and general `Exception` → 500
- [x] `dashboard_trends` catches `ProgrammingError` → 501, `SQLAlchemyError` → 500, and general `Exception` → 500
- [x] `daily_run_counts` catches `ProgrammingError` → 501, `SQLAlchemyError` → 500, and general `Exception` → 500
- [x] All three endpoints log failures via `_log.warning()` or `_log.exception()` with endpoint context
- [ ] `dashboard_summary` caching (`_get_cached_dashboard`) has no error-path test coverage
- [ ] SQLAlchemyError returns 500 instead of 503 — 503 (Service Unavailable) would be more appropriate for transient connection/deadlock failures

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

### Programming error tests

- [x] test_summary_programming_error — ProgrammingError → 501 on summary endpoint
- [x] test_summary_sqlalchemy_error — SQLAlchemyError → 503 on summary endpoint
- [x] test_summary_generic_exception — general Exception → 500 on summary endpoint
- [x] test_trends_programming_error — ProgrammingError → 501 on trends endpoint
- [x] test_trends_sqlalchemy_error — SQLAlchemyError → 503 on trends endpoint
- [x] test_trends_generic_exception — general Exception → 500 on trends endpoint
- [x] test_daily_run_counts_programming_error — ProgrammingError → 501 on daily-run-counts endpoint
- [x] test_daily_run_counts_sqlalchemy_error — SQLAlchemyError → 503 on daily-run-counts endpoint
- [x] test_daily_run_counts_generic_exception — general Exception → 500 on daily-run-counts endpoint

### Resilience & Integration Robustness

- [x] All three endpoints wrap DB queries in `try/except` — degrade gracefully on failure rather than crash
- [x] `dashboard_summary` includes config_warnings section with graceful fallback (broad `except Exception` for model-backend and Remy checks)
- [x] `_log.warning()` with `exc_info=True` on all exception paths — cache read/write handlers now use `exc_info=True` (was passing `%s, exc` instead)
- [ ] No retry/backoff for transient DB failures (deadlock, serialisation) — SQLAlchemyError immediately returns 503
- [ ] No circuit breaker or health check for dashboard-specific DB queries
- [ ] Cache (`_get_cached_dashboard`) has no fallback on Redis/cache failure — cache miss re-queries DB
- [ ] No integration test verifying dashboard endpoints against real DB with migrations applied

## QA History

### 2026-07-06 — Cross-cutting QA (improve-architecture index 232)
- **MAJOR:** Corrected 3 stale product map claims: `daily_run_counts` does catch ProgrammingError → 501 (lines 700-704), `dashboard_trends` also catches SQLAlchemyError → 500 and Exception → 500 (lines 652-662), `dashboard_summary` also catches SQLAlchemyError → 500 and Exception → 500 (lines 438-448)
- **MAJOR:** Created `test_dashboard_programming_error.py` with 9 tests covering ProgrammingError → 501, SQLAlchemyError → 503, and Exception → 500 for all 3 dashboard endpoints
- **MAJOR:** Updated Known Gaps: corrected stale BDD step gap (steps file does not exist — was claimed as empty stubs); removed stale `daily_run_counts` gap
- **MINOR:** Added Resilience section (7 checkboxes: 4 [x] + 3 [ ])
- **MINOR:** Added Error Handling checklist for cache error-path coverage gap
- **MINOR:** Added website docs stub at Website/src/docs/hitl-trends.md

### 2026-07-12 — Round 3 improve-architecture
- **MAJOR:** Fixed B904 (exception chaining) on all 3 dashboard endpoint error handlers — `ProgrammingError`, `SQLAlchemyError`, and `Exception` now use `raise ... from exc` pattern
- **MAJOR:** Fixed `exc_info=True` missing from Redis cache read/write exception handlers (were using `%s, exc` instead of `exc_info=True`)
- **MAJOR:** Corrected stale product map claim — BDD steps file `test_hitl_trends_steps.py` does not exist on disk (was claimed as having empty stubs)

## Known Gaps
- BDD step definitions file (`test_hitl_trends_steps.py`) does not exist — no step definitions are wired for the 7 scenarios in `hitl_trends.feature`. Feature file is spec-only.
- No frontend HITL trend visualisation — API endpoint is fully implemented but has no consuming UI
- Grafana dashboard requires manual import (not provisioned as code)
- No per-team HITL effort breakdown (only org-level in trends endpoint)
- No HITL effort export (CSV, chart image)
- No automated alert on HITL volume spikes or rejection rate thresholds
- `dashboard_summary` caching has no error-path test coverage
- No cache timeout / TTL tests for `_get_cached_dashboard` / `_set_cached_dashboard` 
