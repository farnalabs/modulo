---
id: feat-core-quality-report-slack
prd: 8.6, 8.11
delivery-tasks: [task-nv7-quality-report-slack]
bdd: [backend/tests/bdd/features/connectors/slack_connector.feature ]
code:
  - backend/src/modulo/core/reports/quality_report.py
  - backend/src/modulo/core/reports/__init__.py
depends-on: [feat-core-notifications, feat-evals-eval-engine]
status: partial
---
# Quality Report Slack Delivery Weekly quality report generated from run volume, eval pass rate, and cost data, formatted as Slack Block Kit and delivered to configured Slack webhook URLs. ## Behaviours - [x] `generate_quality_report` queries OrgDailyRunCount for 7-day run volume and total spend
- [x] `generate_quality_report` queries EvalResult for 7-day eval pass rate (passed/total)
- [x] `generate_quality_report` computes week-over-week deltas (runs, eval pass rate, cost)
- [x] `generate_quality_report` builds daily trend array (date, run_count, eval_pass_rate, token_spend_usd)
- [x] `generate_quality_report` produces structured dict with period, summary, week_over_week, trend, eval_breakdown keys
- [x] `_query_weekly_agg` filters by organisation_id and date range, sums run_count and total_spend_usd
- [x] `_query_weekly_agg` filters team_id IS NULL (org-level aggregates only)
- [x] `_query_eval_summary` counts total evals and passed evals with CASE expression
- [x] `_query_daily_eval_rates` groups by date and returns (date, total, passed) tuples
- [x] `_pct_delta` returns None when previous is zero (avoid division by zero)
- [x] `_trend_symbol` returns up/down/flat arrow with 5% threshold and invert option for eval (lower=better inverted)
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
- [x] Error text truncated to 200 characters in delivery results ## Missing / Issues - ~~No API endpoint calls `generate_quality_report` or `deliver_quality_report`~~ — RESOLVED: `POST /{pipeline_id}/quality-report` now triggers report generation and delivery via NotificationEndpoint webhook config (`pipelines.py:569`). Scheduled task still absent.
- No unit tests exist for `quality_report.py`
- No BDD feature files exist for notification webhooks (steps exist at `test_alpha_notifications.py` but `.feature` files are missing)
- Slack connector BDD feature is a placeholder only
- No unit tests for `format_slack_message` output structure or Slack Block Kit schema compliance
- PRD 8.11 says "V1: native Slack" but Slack delivery is already implemented — PRD may be outdated on this point
- PRD 8.11 describes HMAC-SHA256 signing and retry logic for notification webhooks but `deliver_quality_report` does not implement signing, retries, or dead-letter queue
- Scheduled report CRUD exists (ScheduledReport model, CRUD, REST routes in costs.py) but is not connected to quality report generation — only cost reports
- No org-level webhook URL configuration UI or API for quality report recipients
- `deliver_quality_report` accepts `recipient_urls` directly — no persistent endpoint config is consulted ## Known Gaps - API endpoint exists (`POST /{pipeline_id}/quality-report`) but still needs scheduled delivery, comprehensive tests, and dedicated webhook config UI
- No notification bell alert for failed quality report deliveries
- No team-scoped quality reports — always org-wide 