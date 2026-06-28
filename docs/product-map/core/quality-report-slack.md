---
id: feat-core-quality-report-slack
prd: [§8.6, §8.11]
delivery-tasks: [task-nv7-quality-report-slack]
bdd: [backend/tests/bdd/features/connectors/slack_connector.feature (placeholder)]
code:
  - backend/src/modulo/core/reports/quality_report.py
  - backend/src/modulo/core/reports/__init__.py
depends-on: [task-nv1-team-notifications, task-nv2-eval-engine]
status: partial
---

# Quality Report Slack Delivery

Weekly quality report generated from run volume, eval pass rate, and cost data, formatted as Slack Block Kit and delivered to configured Slack webhook URLs.

## Behaviours

- [ ] `generate_quality_report` queries OrgDailyRunCount for 7-day run volume and total spend
- [ ] `generate_quality_report` queries EvalResult for 7-day eval pass rate (passed/total)
- [ ] `generate_quality_report` computes week-over-week deltas (runs, eval pass rate, cost)
- [ ] `generate_quality_report` builds daily trend array (date, run_count, eval_pass_rate, token_spend_usd)
- [ ] `generate_quality_report` produces structured dict with period, summary, week_over_week, trend, eval_breakdown keys
- [ ] `_query_weekly_agg` filters by organisation_id and date range, sums run_count and total_spend_usd
- [ ] `_query_weekly_agg` filters team_id IS NULL (org-level aggregates only)
- [ ] `_query_eval_summary` counts total evals and passed evals with CASE expression
- [ ] `_query_daily_eval_rates` groups by date and returns (date, total, passed) tuples
- [ ] `_pct_delta` returns None when previous is zero (avoid division by zero)
- [ ] `_trend_symbol` returns up/down/flat arrow with 5% threshold and invert option for eval (lower=better inverted)
- [ ] `format_slack_message` returns JSON string of Slack Block Kit blocks
- [ ] Slack blocks include: header, period context, divider, summary fields (total runs, eval pass rate, total cost), WoW deltas section, eval breakdown section, daily trend section, footer
- [ ] Summary shows em-dash when avg_eval_pass_rate is None
- [ ] `deliver_quality_report` POSTs formatted payload to each recipient_url
- [ ] `deliver_quality_report` returns list of per-URL delivery results (url, status, status_code, error)
- [ ] Delivery distinguishes success (2xx) vs failure (non-2xx) by status_code
- [ ] `httpx.RequestError` caught per-URL — one failure does not block other recipients
- [ ] Report period is 7 days (current week) with previous 7-day window for comparison
- [ ] Zero runs in a week produces runs_delta_pct of None (previous=0)
- [ ] Zero evals in a week produces pass_rate of None (total=0)
- [ ] Allowed date range gaps — missing dates produce zero runs and None eval_pass_rate in trend
- [ ] Cost values default to 0.0 when row has no spend data
- [ ] `generate_quality_report` requires an active transaction with RLS org context
- [ ] Webhook delivery uses 30-second timeout per POST
- [ ] Error text truncated to 200 characters in delivery results

## Missing / Issues

- No API endpoint or scheduled task calls `generate_quality_report` or `deliver_quality_report` — functions exist but are orphaned
- No unit tests exist for `quality_report.py`
- No BDD feature files exist for notification webhooks (steps exist at `test_alpha_notifications.py` but `.feature` files are missing)
- Slack connector BDD feature is a placeholder only
- No unit tests for `format_slack_message` output structure or Slack Block Kit schema compliance
- PRD §8.11 says "V1: native Slack" but Slack delivery is already implemented — PRD may be outdated on this point
- PRD §8.11 describes HMAC-SHA256 signing and retry logic for notification webhooks but `deliver_quality_report` does not implement signing, retries, or dead-letter queue
- Scheduled report CRUD exists (ScheduledReport model, CRUD, REST routes in costs.py) but is not connected to quality report generation — only cost reports
- No org-level webhook URL configuration UI or API for quality report recipients
- `deliver_quality_report` accepts `recipient_urls` directly — no persistent endpoint config is consulted

## Known Gaps

- Still needs API endpoint, scheduling, tests, and webhook config persistence
- No notification bell alert for failed quality report deliveries
- No team-scoped quality reports — always org-wide
