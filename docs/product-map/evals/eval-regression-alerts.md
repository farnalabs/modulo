---
id: feat-evals-eval-regression-alerts
prd: 8.17
delivery-tasks: [task-nv7-eval-regression-alerts]
bdd:
  - backend/tests/bdd/features/evals/eval_regex.feature
  - backend/tests/bdd/features/evals/eval_llm_judge.feature
  - backend/tests/bdd/features/evals/eval_block.feature
code:
  - backend/src/modulo/core/eval_engine/regression.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/db/models/eval_result.py
  - backend/tests/unit/core/test_eval_regressions.py
  - backend/tests/unit/api/test_evals_endpoint.py
unit-tests:
  - backend/tests/unit/core/test_eval_regressions.py
depends-on: [feat-evals-eval-engine]
status: covered
---

# Eval Regression Alerts

Detects significant pass-rate drops for eval definitions by comparing a recent window against a baseline window. Emits alerts via the `GET /api/v1/admin/evals/regressions` endpoint (admin-only).

## Behaviours

### Happy paths

- [x] Eval with declining pass rate (drop > threshold) returns alert with trend="declining"
- [x] Eval with improving pass rate (drop <= -threshold) returns alert with trend="improving"
- [x] Eval with stable pass rate (drop within threshold) returns alert with trend="stable"
- [x] Alert includes eval_id, eval_name, prev_pass_rate, current_pass_rate, drop_pct, trend, affected_run_ids
- [x] Empty results (no eval_results in period) returns zero alerts
- [x] Eval with no baseline data (baseline_total=0) is skipped — no alert emitted
- [x] Eval with no recent data (recent_total=0) is skipped — no alert emitted
- [x] Affected_run_ids lists runs with failed results in the recent window
- [x] Pass rates are rounded to 4 decimal places
- [x] Lookback split: baseline uses entire period minus recent_window, recent_window = max(int(days * recent_window_ratio), 1)
- [x] `recent_window_ratio` controls the size of the recent window as a fraction of the lookback period
- [x] `recent_window_ratio` validation: values ≤ 0 or > 1.0 raise `ValueError` (direct) / 422 (API)
- [x] Tiny `recent_window_ratio` clamps the recent window to a minimum of 1 day
- [x] `recent_window_ratio` of 1.0 falls back to half the lookback period (recent window can't equal the whole period)

### Query parameters
- [x] `days` parameter controls total lookback (1–90, default 7)
- [x] `threshold` parameter controls minimum drop fraction (0.0–1.0, default 0.15)
- [x] `recent_window_ratio` parameter controls the recent-window fraction (>0–1.0, default 0.25)
- [x] Custom days (14) and threshold (0.10) produce expected alerts and response metadata
- [x] Custom `recent_window_ratio` (0.5) is echoed back in the response and widens the recent window
- [x] Invalid `recent_window_ratio` (0, negative, >1.0) returns 422
- [x] `pipeline_id` parameter scopes alerts to eval results from runs of a single pipeline (bound into the SQL `runs` join; echoed as `pipeline_id` in the response; absent → `null`)
- [x] `trend` parameter filters alerts to one direction — `declining` surfaces true regressions only, `stable` / `improving` keep those trends (validated; invalid values → 422)
- [x] Invalid `pipeline_id` (non-UUID) returns 422

### Response shape
- [x] Response includes `alerts` list, `total_regressions`, `threshold`, `recent_window_ratio`, `lookback_days`
- [x] Response echoes `pipeline_id` (UUID string or `null`) and `trend` (or `null`) when provided
- [x] Each alert contains exactly 7 keys: eval_id, eval_name, prev_pass_rate, current_pass_rate, drop_pct, trend, affected_run_ids
- [x] eval_id and affected_run_ids are serialised as strings (not UUID objects)

### Error states
- [x] Unauthenticated request returns 401
- [x] Non-admin authenticated user returns 403
- [x] Admin user from another org gets RLS-scoped results (no cross-org data leak)
- [x] Zero results in period returns 200 with empty alerts list (not 404)

### Edge cases
- [x] Drop exactly at threshold boundary: classified as stable (not declining)
- [x] All runs pass in recent window — affected_run_ids is empty
- [x] Single eval with many affected runs — all run IDs collected in affected_run_ids
- [x] Multiple evals with mixed trends return in a single response

### Security
- [x] Endpoint requires admin role — 403 returned for operator/runner
- [x] RLS scopes the SQL query by organisation_id
- [x] Unauthenticated requests return 401 (no dependency override leaks data)

### Backward compatibility
- [x] RegressionAlert dataclass shape matches API response contract
- [x] `detect_regressions` accepts AsyncSession (not coupled to FastAPI)
- [x] Response envelope (alerts + metadata) backward-compatible — `recent_window_ratio` added as an additive field
- [x] `pipeline_id` and `trend` are optional additive params — existing callers passing neither get identical behaviour (org-wide, unfiltered)
- [x] Regression query now `JOIN runs` for pipeline scoping — parameterised so `pipeline_id IS NULL` keeps the query org-wide with no behavioural change
- [x] Invalid `trend` raises `ValueError` in the core layer (direct) / 422 at the API

### Error Handling
- [x] ProgrammingError caught → 501 Not Implemented
- [x] SQLAlchemyError caught → 503 Service Unavailable
- [x] Logged warning on ProgrammingError before returning 501
- [x] Unauthenticated request returns 401
- [x] Non-admin user returns 403

### Additional Edge Cases
- [x] days=1 produces no alerts (baseline window = recent window, evals skipped)
- [x] threshold=0.0 flags every rate change as declining
- [x] threshold=1.0 only flags complete 0%↔100% swings
- [x] Division by zero guarded (recent_total==0 or baseline_total==0 → skip)
- [x] affected_run_ids COALESCE ensures never NULL
- [x] UUID serialised as string in API response

### Resilience & Integration Robustness
- [x] Single SQL query within read transaction — atomic
- [x] RLS enforced via set_rls_org before query
- [x] DB connection failure → ProgrammingError→501
- [x] DB timeout → TimeoutError→503 with user-facing suggestion
- [x] DB connection pool exhaustion → SQLAlchemyError→503
- [x] No retry logic (acceptable for admin-only diagnostic endpoint)

## Known Gaps
- [ ] No BDD feature file specific to regression alerts (covered by unit tests only)
- [ ] No frontend UI to display regression alerts
- [ ] No notification/webhook on regression detection
- [x] ~~**No per-pipeline scoping — query is org-wide only**~~ — **RESOLVED (2026-08-15)**: `detect_regressions()` accepts `pipeline_id` and filters via a `JOIN runs` + parameterised `r.pipeline_id = :pipeline_id` (NULL → org-wide). Exposed as a `pipeline_id` query param on `GET /api/v1/admin/evals/regressions`, echoed in `RegressionAlertsResponse.pipeline_id` (UUID string or `null`). 9 new unit + 3 endpoint tests.
- [ ] No historical trend persistence — each call recomputes from raw eval_results
- [x] ~~**Eval alerts include improving and stable trends — declining-only filter could reduce noise for frontend display**~~ — **RESOLVED (2026-08-15)**: `detect_regressions()` accepts `trend` (`declining`/`stable`/`improving`) and filters alerts after classification; invalid values raise `ValueError`. Exposed as a `trend` query param on the endpoint (422 on invalid), echoed in the response. 4 new unit + 1 endpoint test.

## QA History

### 2026-08-15 — improve-architecture (feature-gap fix)

- **RESOLVED 2 known gaps** in `core/eval_engine/regression.py` + `api/routes/admin.py`:
  1. **Per-pipeline scoping** — `detect_regressions()` now accepts `pipeline_id: UUID | None`. The raw SQL joins `eval_results` → `runs` and filters `(:pipeline_id IS NULL OR r.pipeline_id = :pipeline_id)`, so a NULL pipeline id leaves the query org-wide (no behavioural change) and a given id scopes results to that pipeline's runs. Exposed as `pipeline_id` on `GET /api/v1/admin/evals/regressions` (FastAPI UUID type → non-UUID 422), echoed as a UUID string or `null` in `RegressionAlertsResponse.pipeline_id`.
  2. **Trend filter (declining-only)** — `detect_regressions()` now accepts `trend: str | None` (one of `declining`/`stable`/`improving`, else `ValueError`), filtering alerts after pass-rate classification so e.g. `trend=declining` surfaces true regressions only. Exposed as a `trend` query param (422 on invalid), echoed in `RegressionAlertsResponse.trend`.
- **Tests:** 9 new direct unit tests (pipeline_id bound to query / None-bound, trend filter keeps only matching trend for each of the 3 values, no-match → empty, invalid trend `ValueError` ×4 incl. whitespace/case) + 4 new endpoint tests (pipeline_id echoed + bound, pipeline_id/trend default `null`, trend echoed, invalid trend 422 ×3). Updated the response-shape assertion for the 2 additive fields. 46/46 tests in `test_eval_regressions.py` + 22/22 in `test_evals_endpoint.py` pass.
- **Frontend contract:** regenerated `frontend/src/lib/api/schema.ts` (additive `pipeline_id`/`trend` on `RegressionAlertsResponse` + both query params).
- **Verification:** ruff check + format clean, mypy --strict clean on both modules. Status: covered.

### 2026-08-01 — improve-architecture (feature-gap fix)

- **RESOLVED:** Known gap "No configurable recent window ratio (hardcoded at `max(days // 4, 1)`)" — added `recent_window_ratio: float = 0.25` to `detect_regressions()` (`backend/src/modulo/core/eval_engine/regression.py`), validated (`> 0` and `<= 1.0`, else `ValueError`), with a 1-day floor and the existing half-period fallback when the recent window would span the whole period.
- **API:** exposed `recent_window_ratio` as a `>0–1.0` query parameter on `GET /api/v1/admin/evals/regressions` (default 0.25); echoed back in `RegressionAlertsResponse`.
- **Tests:** added 11 unit/endpoint tests (default-ratio equivalence to legacy behaviour, custom-ratio window shift, 1-day clamp, ratio=1.0 fallback, invalid-ratio `ValueError` ×3, endpoint echo, endpoint 422 ×3). 27/27 tests in `test_eval_regressions.py` pass.
- **Frontend contract:** updated `frontend/src/lib/api/schema.ts` (`RegressionAlertsResponse.recent_window_ratio` + query param).
- Status: covered.
