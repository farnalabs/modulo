---
id: feat-evals-okr-eval-alignment
prd: 8.17
delivery-tasks: [task-nv7-okr-eval-alignment]
bdd:
  - backend/tests/bdd/features/eval/eval_suite_crud.feature
code:
  - backend/src/modulo/core/eval_engine/okr.py
  - backend/src/modulo/api/routes/admin.py
unit-tests:
  - backend/tests/unit/core/test_okr_progress.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/api/test_evals_okr_progress_programming_error.py
depends-on: [feat-evals-eval-definitions, feat-evals-eval-engine, feat-evals-eval-packaging]
status: partial
---

# OKR-Eval Alignment

Mapping eval suites to organisational OKRs so teams can track quality targets over time. Provides pass-rate bucketing into configurable time windows, trend direction detection, breach alerts, and an admin progress endpoint.

## Behaviours

### Suite-to-OKR Mapping
- [ ] Suite created with pass_threshold and optional target_date, owner fields
- [ ] Suite tagged with an OKR identifier for multi-suite grouping
- [ ] Multiple suites can share the same OKR ID
- [ ] OKR progress summary aggregated across all suites for a given OKR
- [ ] Suites without an OKR ID are still trackable individually

### Progress Tracking
- [x] track_okr_progress() buckets pass rates into sequential time windows (7d, 14d, 30d, overall)
- [x] Trend direction detection: declining / stable / improving from two most recent non-empty periods
- [x] Breach alert when current pass rate falls below pass_threshold
- [x] Admin endpoint: GET /api/v1/admin/evals/okr-progress/{suite_id}
- [ ] OKR progress endpoint supports aggregate flag to return OKR-level rollup across suites
- [x] OKR progress returned with suite_id, suite_name, current_score, pass_threshold, trend, trend_direction, days_to_target, breach

### Target Date Management
- [x] target_date parsing failure returns None days_to_target (graceful degradation)
- [ ] Suite target_date persisted as a DB column (currently Pydantic-only on OkrSuite)
- [ ] days_to_target computed from now to ISO 8601 target_date
- [ ] Target date in the past returns days_to_target = 0
- [ ] No target_date returns days_to_target = None

### Breach Detection & Notification
- [x] alert_on_breach returns True when current_pass_rate < pass_threshold
- [x] alert_on_breach returns False at or above threshold
- [x] alert_on_breach_for_suite convenience wrapper using OkrSuite model
- [ ] Breach triggers a notification event (webhook or in-app alert)
- [ ] Breach notification includes suite_id, current_score, threshold, trend direction
- [ ] Scheduled quality report includes OKR breach summary
- [ ] Multiple breaches in a single check produce one notification per breached suite

### Auth & Access Control
- [x] Unauthenticated requests return 401 on OKR progress endpoint
- [x] Non-admin users receive 403 on OKR progress endpoint
- [x] RLS scopes OKR progress queries by organisation_id
- [x] OKR progress endpoint scoped to admin-only in alpha

### Edge Cases
- [x] Suite not found in DB raises ValueError (returned as 404)
- [x] Fewer than 2 periods with data → trend direction is stable
- [x] No threshold on suite → breach is always False
- [x] No eval results yet → current_score = 0.0, breach flagged if threshold is > 0
- [ ] Suite with zero definitions — progress query returns no results
- [ ] Suite_id contains special characters or Unicode — endpoint handles encoding
- [ ] Organisation with no suites at all — empty progress response
- [ ] Large number of suites under one OKR — aggregation paginates or caps

### Error Handling

- [x] Unauthenticated requests return 401 on OKR progress endpoint
- [x] Non-admin users receive 403 on OKR progress endpoint
- [x] Suite not found in DB raises ValueError → returned as 404
- [x] Database ProgrammingError caught → returned as 501 Not Implemented with migration hint
- [x] target_date parsing failure returns None days_to_target (graceful degradation)
- [ ] SQLite dialect incompatibility — raw SQL text() queries use PostgreSQL-specific SQL

### Testing

- [x] Unit tests for track_okr_progress() — 14 tests covering: missing suite, returns model, trend periods, correct values, breach detection, no threshold, target date, trending, no data
- [x] Unit tests for alert_on_breach() — 7 tests covering: below, above, at threshold, zero threshold, zero rate, perfect rate, suite wrapper
- [x] Unit tests for _compute_trend_direction() — 6 tests covering: single point, empty, declining, improving, stable, skips empty periods
- [x] Unit tests for _days_between() — 5 tests covering: None target, future date, past date, invalid string, today
- [x] Unit tests for OkrSuite model — 2 tests covering: minimal, full
- [x] Unit tests for OkrProgress model — 1 test covering all fields
- [x] API endpoint tests — 7 tests covering: 401, 200, response shape, trend points, correct data, trend values, target_date param, missing suite 404, breach true
- [x] ProgrammingError → 501 unit test — 1 test covering the error path
- [ ] Suite-level aggregation unit tests — 7 tests in test_eval_suite.py covering passing suite, at threshold, failing suite, no threshold, mixed results, empty suite, model fields

### BDD Coverage
- [x] eval_suite_crud.feature — 8 real scenarios exist with step definitions in test_eval.py (CREATE, LIST, GET, UPDATE, DELETE, auth checks)
- [ ] Scenario: Admin views OKR progress for a suite over time
- [ ] Scenario: Suite breaches threshold — alert surfaces in dashboard
- [ ] Scenario: Non-admin receives 403 when accessing OKR progress
- [ ] Scenario: Multiple suites under one OKR show aggregate progress

## Known Gaps

- No dedicated suite DB entity — suite_id is a free-form string on eval_definitions, no FK, no metadata (prevents OKR-level aggregation)
- No OKR identifier column on eval_definitions — cross-suite OKR aggregation not queryable
- OkrSuite.target_date and owner fields are Pydantic-only — not persisted to DB
- No OKR progress visualization/dashboard UI exists
- No breach notification mechanism (webhook, in-app, or email) — breach is detected but alert not delivered
- No scheduled quality report support — OKR breach summary not surfaced anywhere
- No multi-suite aggregation endpoint (/evals/okr-progress?okr_id=...) — can only query by single suite_id
- Raw SQL `text()` queries use PostgreSQL-specific SQL — will fail on SQLite (test/development) with ProgrammingError (already caught as 501)
- BDD coverage is incomplete — no scenarios explicitly test the OKR progress endpoint, only eval_suite_crud.feature (eval definition CRUD, not progress tracking)
- Elena persona scenario 8 ("Create eval suites aligned with team OKRs") not verified end-to-end

## QA History

### 2026-07-04 — Cross-cutting architecture QA (index 133)

**Findings fixed:**
- MAJOR: Fixed 2 stale `[ ]` behaviour checkboxes → `[x]`: (1) OKR progress response shape (all 8 fields are returned by the endpoint), (2) Admin-only scoping (already enforced by 403 check in route handler — checkbox was `[ ]` despite being implemented).
- MAJOR: Fixed stale BDD claim — eval_suite_crud.feature is NOT a placeholder; it has 8 real Gherkin scenarios with step definitions wired in test_eval.py. Replaced the inaccurate claim with accurate checkbox.
- MAJOR: Added Error Handling section with 6 behaviour checkboxes covering all error paths (401, 403, 404, 501, target_date graceful degradation, SQLite dialect gap).
- MAJOR: Added Testing section with 10 behaviour checkboxes documenting all unit test coverage (track_okr_progress: 14 tests, alert_on_breach: 7, compute_trend_direction: 6, days_between: 5, models: 3, API endpoint: 9, ProgrammingError: 1, suite aggregation: 7).
- MINOR: Refined Known Gaps — removed stale "placeholder" BDD claim, added SQLite dialect gap, added breach notification gap, added missing BDD coverage for OKR progress endpoint.
- MINOR: Updated `unit-tests` frontmatter — added `test_evals_okr_progress_programming_error.py` ref (was missing even though file exists at backend/tests/unit/api/).

### Summary
Status: partial (10 known gaps remain — all infrastructure/features, not code correctness issues). Existing 52+ unit tests confirmed on disk. No code changes needed — only product map documentation. 