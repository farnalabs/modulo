---
id: feat-core-quality-report-slack
prd: 8.6, 8.11
delivery-tasks: [task-nv7-quality-report-slack]
bdd:
  - backend/tests/bdd/features/connectors/slack_connector.feature
  - backend/tests/bdd/features/reports/quality_report.feature
  - backend/tests/bdd/features/reports/quality_report_delivery.feature
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
- [x] `_trend_symbol` `invert` option flips the arrow for metrics where lower is better (cost line renders up arrow on decrease, down arrow on increase) — wired into `_format_trend_section` for the cost delta
- [x] `format_slack_message` returns JSON string of Slack Block Kit blocks
- [x] Slack blocks include: header, period context, divider, summary fields (total runs, eval pass rate, total cost), WoW deltas section, eval breakdown section, daily trend section, footer
- [x] `format_slack_message` output conforms to Slack Block Kit limits (≤50 blocks, header ≤150 chars, section text ≤3000 chars, ≤10 fields each ≤2000 chars, ≤10 context elements each ≤3000 chars)
- [x] Summary shows em-dash when avg_eval_pass_rate is None
- [x] `deliver_quality_report` POSTs formatted payload to each recipient_url
- [x] `deliver_quality_report` returns list of per-URL delivery results (url, status, status_code, error)
- [x] Delivery distinguishes success (2xx) vs failure (non-2xx) by status_code
- [x] `httpx.RequestError` caught per-URL — one failure does not block other recipients
- [x] Delivery payload is HMAC-SHA256 signed (`X-Modulo-Signature: sha256=<hex>` header) when `signing_secret` is configured — body serialized once and sent byte-for-byte so the signature is verifiable against the received body (PRD 8.11)
- [x] Delivery without `signing_secret` omits the signature header and sends the body via `json=`
- [x] Delivery per-request timeout is caller-configurable via `timeout` recipient-config key (default 30s) — invalid/zero values fall back to the default
- [x] Report period is 7 days (current week) with previous 7-day window for comparison
- [x] Zero runs in a week produces runs_delta_pct of None (previous=0)
- [x] Zero evals in a week produces pass_rate of None (total=0)
- [x] Allowed date range gaps — missing dates produce zero runs and None eval_pass_rate in trend
- [x] Cost values default to 0.0 when row has no spend data
- [x] `generate_quality_report` requires an active transaction with RLS org context
- [x] Webhook delivery uses 30-second timeout per POST (default; caller-configurable)
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
- ~~No unit tests for `format_slack_message` output structure or Slack Block Kit schema compliance~~ — RESOLVED (2026-08-13): `TestSlackBlockKitSchema` in `backend/tests/unit/reports/test_quality_report.py` asserts block count ≤50, header ≤150 chars, section text ≤3000 chars, ≤10 fields each ≤2000 chars, and ≤10 context elements each ≤3000 chars across populated/empty reports, plus the exact block-type sequence.
- ~~PRD 8.11 describes HMAC-SHA256 signing ... but `deliver_quality_report` does not implement signing ...~~ — RESOLVED (2026-08-13): HMAC-SHA256 signing implemented (see Known Gaps).
- ~~No timeout configuration exposed to callers~~ — RESOLVED (2026-08-13): `timeout` recipient-config key (see Known Gaps).

### Remaining
- PRD 8.11 says "V1: native Slack" but Slack delivery is already implemented — PRD may be outdated on this point
- PRD 8.11 dead-letter queue for failed deliveries is not implemented (retry logic exists in `_deliver_to_urls`: 3 attempts, expo backoff, 429 handling)
- Scheduled report CRUD exists (ScheduledReport model, CRUD, REST routes in costs.py) but is not connected to quality report generation — only cost reports
- No org-level webhook URL configuration UI or API for quality report recipients
- `deliver_quality_report` accepts `recipient_urls` directly — no persistent endpoint config is consulted
- API endpoint exists (`POST /{pipeline_id}/quality-report`) but still needs scheduled delivery, comprehensive tests, and dedicated webhook config UI
- No notification bell alert for failed quality report deliveries
- No team-scoped quality reports — always org-wide

## Known Gaps

- Scheduled delivery infrastructure exists (DatabaseReportScheduler, quality report type registered in reports/__init__.py) but no user-facing API or UI to create quality-type scheduled reports
- ~~No HMAC-SHA256 signing of webhook payloads~~ — **RESOLVED (2026-08-13)**: `_deliver_to_urls` accepts a `signing_secret` (threaded through `deliver_quality_report`, `_deliver_slack_webhook`, `_deliver_webhook`, `_deliver_via_config`). When set, the body is serialized once via `_serialize_json_body()` (compact separators + sorted keys), signed with `_sign_payload()` (`HMAC-SHA256` over the exact bytes, `X-Modulo-Signature: sha256=<hex>` header — a plain HMAC over the raw body, not Slack's `v0:timestamp:body` scheme), and POSTed via `content=` so the signed bytes are byte-for-byte the received bytes — a recipient can recompute the signature over the raw request body. Unsigned (or empty-secret) deliveries are unchanged (`json=` + no signature header). Unit tests: `TestWebhookSigning` (byte-stable serialization, known-vector signature, signed-delivery headers/bytes/`Content-Type`, unsigned `json=` path, verifiable-from-raw-body recomputation, empty-secret → unsigned) + 2 BDD scenarios in `quality_report_delivery.feature`.
- No dead-letter queue for failed deliveries (retry logic exists: 3 attempts, exponential backoff, 429 retry-after in `_deliver_to_urls`)
- No org-level webhook URL configuration UI
- No team-scoped quality reports

## Error Handling

- [x] `ProgrammingError` caught in `trigger_quality_report` — returns 501 Not Implemented
- [x] `httpx.RequestError` caught per-URL in `_deliver_to_urls` — single failure doesn't block others
- [x] Error text truncated to 200 characters in delivery results
- [x] Non-2xx webhook responses distinguished from HTTP errors in delivery results
- [x] Invalid cron expressions caught in `_fire_scheduled_report` — auto-deactivates the report
- [x] `json.JSONDecodeError` handled when parsing notification endpoint events (line 865)
- [x] Retry logic exists in `_deliver_to_urls` — 3 attempts, exponential backoff, 429 handling with Retry-After
- [ ] No dead-letter queue for persistently failing webhook URLs
- [x] Delivery timeout is caller-configurable via the `timeout` recipient-config key (default 30s in `_deliver_to_urls`) — invalid or zero values fall back to the default
- [ ] No health check or pre-flight validation of webhook URLs before delivery
- [x] `generate_quality_report` wraps DB queries in try/except (SQLAlchemyError + Exception) — non-DB errors are caught and logged

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
- [ ] Sequential URL delivery — `_deliver_to_urls` reuses a single httpx client for all URLs; a slow/blocking URL delays subsequent deliveries
- [x] Delivery timeout is caller-configurable via the `timeout` recipient-config key (default 30s) — a single value still applies to all URLs, but is no longer hardcoded

## QA History

### 2026-08-13 — improve-tests: QA lens pass on the reports scheduler test package

**Closed the remaining branch gaps in `core/reports/scheduler.py`** (the shared report fire/delivery engine also imported by `cron_helpers` fire jobs) with 19 new tests in `tests/unit/reports/test_report_scheduler.py`. The caller-supplied timeout validation gate `_coerce_timeout` had **zero direct coverage** — the gate exists to reject bools/zeros/negatives/non-numerics that would otherwise silently become a 1s timeout (`float(True) == 1.0`). Coverage after the pass: `scheduler.py` **100% line coverage**, only the intentionally-unreachable double-checked-lock race branch (`_get_engine` 100->113) remains partial (99% branch).

**Lens findings locked:**
- `_coerce_timeout` truth table: positive int/float/numeric-string accepted; bool `True`/`False`, `0`, negatives, `"0"`, `"abc"`, `None`, and arbitrary objects all rejected → `None`.
- End-to-end through `_deliver_to_urls`: a `request_timeout` of `True`, `0`, `-5`, `"abc"`, or `None` must fall back to the default `_REPORT_HTTP_TIMEOUT` (30s) on the underlying `httpx.AsyncClient` — never `1.0` from `float(True)`. Valid `2.5`/`15` honored exactly.
- End-to-end through `_deliver_via_config`: `{"timeout": true}` in a recipient config reaches the client as the 30s default, while `{"timeout": 4.25}` is honored — proving the bool guard holds on the config path, not just on the direct helper.

**Tests:** 19 new tests in `tests/unit/reports/test_report_scheduler.py` (`TestCoerceTimeout` ×11, `TestDeliverToUrlsTimeout` ×7, `TestDeliverViaConfig::test_timeout_passes_through_recipient_config` ×1). 129/129 reports unit tests pass; ruff check + format clean.

### 2026-08-13 — improve-architecture (product-map walk)

**RESOLVED 4 known gaps / unchecked behaviours in `feat-core-quality-report-slack`:**

1. **HMAC-SHA256 webhook signing (PRD 8.11)** — `_deliver_to_urls` accepts a `signing_secret` (threaded through `deliver_quality_report`, `_deliver_slack_webhook`, `_deliver_webhook`, `_deliver_via_config`). The body is serialized once (`_serialize_json_body`: compact separators + `sort_keys=True`) and signed (`_sign_payload`: `sha256=<hex>` header — a plain HMAC over the raw body, not Slack's `v0:timestamp:body` scheme), then POSTed via `content=` so the signed bytes are exactly the received bytes — a recipient recomputes the signature over the raw request body. Unsigned (or empty-secret) deliveries keep the `json=` path with no signature header. `_deliver_to_urls`/`_deliver_slack_webhook` parameter renamed `timeout` → `request_timeout` to satisfy ruff ASYNC109.
2. **Caller-configurable delivery timeout** — `timeout` recipient-config key (default 30s); `_coerce_timeout` falls back to the default for invalid/zero/bool values.
3. **`_trend_symbol` invert option** — new `invert: bool = False` parameter (semantics: negative delta = improvement = up arrow); wired into `_format_trend_section` for the cost line so the WoW section is semantically consistent across metrics (cost up → down arrow, cost down → up arrow).
4. **Slack Block Kit schema compliance** — `TestSlackBlockKitSchema` asserts block count ≤50, header ≤150 chars, section text ≤3000 chars, ≤10 fields each ≤2000 chars, ≤10 context elements each ≤3000 chars, and the exact block-type sequence.

**Tests:** added 6 unit tests for signing helpers + delivery signing (`TestWebhookSigning` ×2 + 4 signed/unsigned delivery tests) + 3 timeout tests + 4 invert tests + 2 cost-line trend tests + 7 Block Kit schema tests in `tests/unit/reports/test_quality_report.py`; new `tests/bdd/features/reports/quality_report_delivery.feature` (5 scenarios) with step definitions in `tests/bdd/steps/reports/test_quality_report_delivery.py`. 125/125 reports unit + BDD tests pass, ruff check + format clean, mypy --strict clean. Status: partial (dead-letter queue, org-level webhook config UI, team-scoped reports, "no data" Slack message remain).

### 2026-07-07 — Cross-cutting QA (current)

**Findings fixed:**
- MAJOR: Error handling checkbox `generate_quality_report has no try/except` was stale — code DOES have try/except Exception. Marked `[x]`.
- MAJOR: Known Gaps claimed "Scheduled delivery not yet implemented" — scheduler infrastructure EXISTS (DatabaseReportScheduler, registered quality report type). Updated to reflect actual gap: no user-facing API/UI for quality-type scheduled reports.
- MAJOR: Known Gaps claimed "No retry logic" — `_deliver_to_urls` HAS 3-attempt retry with expo backoff and 429 handling. Updated description.
- MAJOR: Error Handling checkbox "No retry logic" was unchecked — retry logic exists. Marked `[x]`.
- MAJOR: "Remaining" section claimed deliver_quality_report doesn't implement retries — inaccurate. Updated to reflect actual gaps (signing, dead-letter).
- MINOR: Added edge cases for sequential URL delivery (single httpx client shared) and hardcoded timeout.

### 2026-07-05 — Cross-cutting QA

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
