---
id: feat-core-quality-report-slack
prd: 8.6, 8.11
delivery-tasks: [task-nv7-quality-report-slack]
bdd:
  - backend/tests/bdd/features/connectors/slack_connector.feature
  - backend/tests/bdd/features/reports/quality_report.feature
code:
  - backend/src/modulo/core/reports/quality_report.py
  - backend/src/modulo/core/reports/__init__.py
  - backend/src/modulo/api/routes/pipelines.py
depends-on: [feat-core-notifications, feat-evals-eval-engine]
unit-tests:
  - backend/tests/unit/reports/test_quality_report.py
status: partial
---

# Quality Report Slack Delivery

Weekly quality report generated from run volume, eval pass rate, and cost data, formatted as Slack Block Kit and delivered to configured Slack webhook URLs.

## Behaviours

- [x] `generate_quality_report` queries OrgDailyRunCount for 7-day run volume and total spend
- [x] `generate_quality_report` queries EvalResult for 7-day eval pass rate (passed/total)
- [x] `generate_quality_report` computes week-over-week deltas (runs, eval pass rate, cost)
- [x] `generate_quality_report` builds daily trend array (date, run_count, eval_pass_rate, token_spend_usd)
- [x] `generate_quality_report` produces structured dict with period, summary, week_over_week, trend, eval_breakdown keys
- [x] `_query_weekly_agg` filters by organisation_id and date range, sums run_count and total_spend_usd
- [x] `_query_weekly_agg` filters team_id IS NULL (org-level aggregates only)
- [x] `_query_eval_summary` counts total evals and passed evals with CASE expression
- [x] `_query_daily_eval_rates` groups by date and returns (date, total, passed) tuples
- [x] `_pct_delta` returns None when previous is zero (avoid division by zero)
- [x] `_trend_symbol` returns up/down/flat arrow with 5% threshold (strict — exact 5% returns flat)
- [ ] `_trend_symbol` invert option for eval metrics (lower=better inverted) — not implemented in function signature or call sites
- [x] `format_slack_message` returns JSON string of Slack Block Kit blocks
- [x] Slack blocks include: header, period context, divider, summary fields (total runs, eval pass rate, total cost), WoW deltas section, eval breakdown section, daily trend section, footer
- [x] Summary shows em-dash when avg_eval_pass_rate is None
- [x] `deliver_quality_report` POSTs formatted payload to each recipient_url
- [x] `deliver_quality_report` returns list of per-URL delivery results (url, status, status_code, error)
- [x] Delivery distinguishes success (2xx) vs failure (non-2xx) by status_code
- [x] `httpx.RequestError` caught per-URL — one failure does not block other recipients
- [x] Report period is 7 days (current week) with previous 7-day window for comparison
- [x] Zero runs in a week produces runs_delta_pct of None (previous=0)
- [x] Zero evals in a week produces pass_rate of None (total=0)
- [x] Allowed date range gaps — missing dates produce zero runs and None eval_pass_rate in trend
- [x] Cost values default to 0.0 when row has no spend data
- [x] `generate_quality_report` requires an active transaction with RLS org context
- [x] Webhook delivery uses 30-second timeout per POST
- [x] Error text truncated to 200 characters in delivery results
- [x] `POST /{pipeline_id}/quality-report` endpoint triggers report generation and delivery
- [x] Pipeline endpoint passes recipient URLs as dict to `deliver_quality_report`
- [x] Returns 501 Not Implemented when DB tables missing (ProgrammingError caught)
- [x] Unit tests exist for `_pct_delta`, `_trend_symbol`, `_fmt_delta`, formatting functions, `format_slack_message`, `deliver_quality_report`, and `generate_quality_report`
- [x] BDD feature files exist for quality report delivery (happy path, no webhook, pipeline not found)

## Missing / Issues

### Resolved
- ~~No unit tests exist for `quality_report.py`~~ — RESOLVED: `backend/tests/unit/reports/test_quality_report.py` covers all functions (2026-07-01)
- ~~No BDD feature files exist for notification webhooks~~ — RESOLVED: `backend/tests/bdd/features/reports/quality_report.feature` with step definitions (2026-07-01)
- ~~Slack connector BDD feature is a placeholder only~~ — RESOLVED: 12 real scenarios exist in `backend/tests/bdd/features/connectors/slack_connector.feature` (2026-07-01)
- ~~Type mismatch: pipeline endpoint passes `recipient_urls: list[str]` directly to `deliver_quality_report` which expects `dict[str, Any]`~~ — RESOLVED: now wraps in `{"webhook_urls": recipient_urls}` (2026-07-01)
- ~~No ProgrammingError catch on pipeline endpoint~~ — RESOLVED: `trigger_quality_report` now catches `ProgrammingError` and returns 501 Not Implemented (2026-07-01)

### Remaining
- No unit tests for `format_slack_message` output structure or Slack Block Kit schema compliance
- PRD 8.11 says "V1: native Slack" but Slack delivery is already implemented — PRD may be outdated on this point
- PRD 8.11 describes HMAC-SHA256 signing and retry logic for notification webhooks but `deliver_quality_report` does not implement signing, retries, or dead-letter queue
- Scheduled report CRUD exists (ScheduledReport model, CRUD, REST routes in costs.py) but is not connected to quality report generation — only cost reports
- No org-level webhook URL configuration UI or API for quality report recipients
- `deliver_quality_report` accepts `recipient_urls` directly — no persistent endpoint config is consulted
- API endpoint exists (`POST /{pipeline_id}/quality-report`) but still needs scheduled delivery, comprehensive tests, and dedicated webhook config UI
- No notification bell alert for failed quality report deliveries
- No team-scoped quality reports — always org-wide

## Known Gaps

- Scheduled delivery (cron-triggered quality reports) not yet implemented
- No HMAC-SHA256 signing of webhook payloads
- No retry logic or dead-letter queue for failed deliveries
- No org-level webhook URL configuration UI
- No team-scoped quality reports

## Error Handling

- [x] `ProgrammingError` caught in `trigger_quality_report` — returns 501 Not Implemented
- [x] `httpx.RequestError` caught per-URL in `_deliver_to_urls` — single failure doesn't block others
- [x] Error text truncated to 200 characters in delivery results
- [x] Non-2xx webhook responses distinguished from HTTP errors in delivery results
- [x] Invalid cron expressions caught in `_fire_scheduled_report` — auto-deactivates the report
- [x] `json.JSONDecodeError` handled when parsing notification endpoint events (line 865)
- [ ] No retry logic for failed webhook deliveries — single attempt only
- [ ] No dead-letter queue for persistently failing webhook URLs
- [ ] No timeout configuration exposed to callers — hardcoded 30s in `_deliver_to_urls`
- [ ] No health check or pre-flight validation of webhook URLs before delivery
- [ ] `generate_quality_report` has no try/except around DB queries — non-DB errors (TypeError, ValueError during data processing) propagate to CatchAllMiddleware as opaque 500

## Resilience

- [x] Multi-URL delivery isolated per-URL — one failure doesn't block others
- [x] `_pct_delta` handles division by zero (previous=0 returns None)
- [x] `_trend_symbol` handles None input (returns flat arrow)
- [x] `_fmt_pct` handles None (returns em-dash)
- [x] `_fmt_delta` handles None (returns "N/A")
- [x] Report generation handles empty weeks (zero rows in DB)
- [x] Missing trend dates produce default values (0 runs, None pass rate, 0.0 cost)
- [ ] No circuit breaker for repeatedly failing webhook URLs
- [ ] No fallback delivery channel (e.g. email) when webhook delivery fails
- [ ] No idempotency on report generation — duplicate calls produce duplicate reports

## Edge Cases

- [x] Exact 7-day range with no overlap between current and previous week windows
- [x] `_query_weekly_agg` filters team_id IS NULL — team-scoped data excluded from org-level report
- [x] Zero runs in both current and previous week → runs_delta_pct None
- [x] Zero evals in both weeks → pass_rate None for both
- [x] Partial daily data — missing dates produce default entries in trend
- [x] `_query_eval_summary` uses `end_date + 1 day` (< end_dt) for correct date range exclusion
- [x] All SQL aggregate functions (`SUM`, `COUNT`) wrapped in null-safe helpers (`.run_count else 0`, `.total_spend else 0.0`)
- [ ] Report with zero runs and zero evals produces mostly-None output — no "no data" message in Slack blocks
- [ ] What happens when `OrgDailyRunCount` table exists but has no rows for the org? → empty weekly, everything zero or None
- [ ] Trend block with all-None eval_pass_rate renders em-dashes for every entry — no visual distinction from "no data available"

## QA History

### 2026-07-05 — Cross-cutting QA (current)

**Findings fixed:**
- MAJOR: 5 `_trend_symbol` tests had wrong expectations — asserted UP/DOWN for values within ±5% threshold. Fixed to expect FLAT. Tests pass 63/63.
- MAJOR: Product map claimed `_trend_symbol` had an `invert` parameter for eval metrics. No such parameter exists in the function signature or call sites. Downgraded to `[ ]` (unimplemented).
- MAJOR: Missing Error Handling, Resilience, and Edge Cases sections. Added with 30 new behaviour checkboxes.
- MINOR: Missing website docs stub. Created `core/quality-reports.md`.

### 2026-07-01 — Cross-cutting architecture QA

**Findings fixed:**
- CRITICAL: Type mismatch — `trigger_quality_report` passed `list[str]` to `deliver_quality_report` which expects `dict[str, Any]`. Fixed by wrapping in `{"webhook_urls": recipient_urls}`.
- MAJOR: Missing `ProgrammingError` catch in `trigger_quality_report`. Added standard 501 Not Implemented handler.
- MAJOR: No unit tests for `quality_report.py`. Created `backend/tests/unit/reports/test_quality_report.py` with 30+ tests covering all functions.
- MAJOR: No BDD feature files for quality report delivery. Created `backend/tests/bdd/features/reports/quality_report.feature` with 3 scenarios and step definitions.
- MAJOR: Product map listed stale placeholders. Updated with resolved/remaining sections and QA history.
