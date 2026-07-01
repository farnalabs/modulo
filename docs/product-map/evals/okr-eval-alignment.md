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
- [ ] OKR progress returned with suite_id, suite_name, current_score, pass_threshold, trend, trend_direction, days_to_target, breach

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
- [ ] OKR progress endpoint scoped to admin-only in alpha

### Edge Cases
- [x] Suite not found in DB raises ValueError (returned as 404)
- [x] Fewer than 2 periods with data → trend direction is stable
- [x] No threshold on suite → breach is always False
- [x] No eval results yet → current_score = 0.0, breach flagged if threshold is > 0
- [ ] Suite with zero definitions — progress query returns no results
- [ ] Suite_id contains special characters or Unicode — endpoint handles encoding
- [ ] Organisation with no suites at all — empty progress response
- [ ] Large number of suites under one OKR — aggregation paginates or caps

### BDD Coverage
- [ ] eval_suite_crud.feature — placeholder only (scenarios not implemented)
- [ ] Scenario: Admin creates a suite and sets pass_threshold
- [ ] Scenario: Admin views OKR progress for a suite over time
- [ ] Scenario: Suite breaches threshold — alert surfaces in dashboard
- [ ] Scenario: Non-admin receives 403 when accessing OKR progress
- [ ] Scenario: Multiple suites under one OKR show aggregate progress

## Known Gaps
- No dedicated suite DB entity — suite_id is a free-form string on eval_definitions, no FK, no metadata
- No OKR identifier column on eval_definitions — cross-suite OKR aggregation not queryable
- OkrSuite.target_date and owner fields are Pydantic-only — not persisted to DB
- eval_suite_crud.feature is a placeholder — no BDD scenarios implemented
- No OKR progress visualization/dashboard UI exists
- No breach notification mechanism (webhook, in-app, or email)
- No scheduled quality report support — OKR breach summary not surfaced anywhere
- No multi-suite aggregation endpoint (/evals/okr-progress?okr_id=...)
- Elena persona scenario 8 ("Create eval suites aligned with team OKRs") not verified end-to-end 