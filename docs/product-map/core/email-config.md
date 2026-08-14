---
id: feat-core-email-config
prd: 8.11
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/admin_email.py
  - backend/src/modulo/core/email_service.py
bdd:
  - backend/tests/bdd/features/admin/email-settings.feature
unit-tests:
  - backend/tests/unit/api/test_admin_email.py
  - backend/tests/unit/core/test_email_service.py
depends-on: [feat-core-notifications]
status: partial
---

# Email Configuration

Organisation-level SMTP email configuration for sending transactional emails. Settings are stored in the organisation's `settings_json` blob and include SMTP host, port, credentials, and sender address. Supports a test-send endpoint to validate configuration.

## Behaviours

- [x] GET email settings from org settings_json
- [x] PUT email settings (merge with existing)
- [x] Clear SMTP password on demand
- [x] POST test email to validate SMTP config
- [x] Admin-only access (org admin or system admin)
- [x] SMTP not configured returns 422 on test
- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] Test-send success returns `{"ok": true}` with confirmation message
- [x] Test-send SMTP failure returns `{"ok": false}` with descriptive reason
- [x] Test-send unexpected exception returns generic message (never leaks internals)
- [x] Network failures (connection refused, timeout, DNS) retried then wrapped as `EmailSendingError`
- [x] SMTP credentials redacted from error messages and logs (`********` replaces username/password)
- [x] Configurable SMTP timeout — org-level `smtp_timeout` (default 30s, validated 1–120s, 422 outside range) used for SMTP connect/send; malformed values fall back to 30s
- [x] SMTP password length limit — `smtp_password` max 256 chars enforced at the API (422 on exceed)
- [x] Test-send recipient validated — malformed addresses (no `@`, display names, URLs, header-injection CR/LF, multi-recipient) rejected with 422 before any SMTP attempt
- [x] Test-send rate limited per org — 3 test emails per 60-minute window; exceeding returns 429 with `Retry-After`; budgets are per-org isolated
- [ ] Email templates configuration
- [ ] Per-organisation email branding

## Error Handling

- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] SMTP not configured returns 422 on test-send
- [x] Invalid SMTP host/port returns 422 validation error
- [x] SMTP connection failure during test returns descriptive error with failure reason
- [x] Password clear when no password stored returns 200 (idempotent)
- [x] Unexpected exception in test-send returns generic "Unexpected error while sending the test email" — raw exception text is never echoed to the client
- [x] Malformed test-send recipient returns 422 before any SMTP attempt
- [x] Test-send rate limit exceeded returns 429 with `Retry-After` header

## Edge Cases

- [x] Empty SMTP host returns 422
- [x] Invalid port number returns 422
- [x] Missing email settings returns 501 (ProgrammingError for missing DB table)
- [x] SMTP auth failure redacts the configured username/password from the returned message (was leaking credentials in error message)
- [x] Network-level failures (connection refused, timeout) are retried up to 3 attempts before raising `EmailSendingError` (was escaping uncaught as raw `OSError`)
- [x] SMTP timeout configurable per org (default 30s) — non-numeric/zero/oversized values clamped to the safe 30–120s range instead of crashing send
- [x] Oversized SMTP password (>256 chars) rejected with 422 before any DB write
- [x] Test-send recipient with header-injection characters (CR/LF, `Bcc:`) rejected with 422
- [x] Test-send rate limit budget is per-org — exhausting one org's budget does not affect another org's test-send
- [ ] Concurrent PUT of email settings — last-write-wins on `settings_json`

## Security

- [x] Admin-only access (org admin role required)
- [x] SMTP password stored in settings_json — masked in GET responses
- [x] SMTP credentials redacted from SMTP error messages before reaching logs or clients
- [x] Test-send relay abuse bounded — per-org rate limiting (3/60min) plus recipient validation prevent arbitrary-recipient relay enumeration
- [ ] No encryption for SMTP password at rest (stored in plain JSON column)

## Known Gaps

- **Concurrent PUT is last-write-wins** — no optimistic locking on `settings_json` for email settings.
- **SMTP password at rest** — stored in the plain `settings_json` JSON column; no encryption.
- ~~**Test-send relay abuse** — the endpoint sends mail to an arbitrary recipient address; no rate limiting or recipient allowlisting.~~ **RESOLVED 2026-08-14**: the test-send endpoint now rejects malformed recipients (header-injection CR/LF, display names, URLs, multi-recipient, no-`@`) with 422 before any SMTP attempt, and enforces a per-org test-send budget (`EmailSendLimiter` in `core/email_service.py`: 3 sends per 60-minute fixed window, per-org `asyncio.Lock`, injectable clock, fail-open on limiter errors) returning 429 with `Retry-After` when exhausted. 8 new `test_email_service.py` limiter/validation tests + 4 new `test_admin_email.py` route tests + 4 new BDD scenarios in `email-settings.feature`.

## QA History

- 2026-08-14: improve-architecture: **RESOLVED the "Test-send relay abuse" gap** (`core/email_service.py` + `api/routes/admin_email.py`). (1) **Recipient validation** — the test-send endpoint previously relayed to an arbitrary recipient string with only `min_length=1`; the recipient is now validated by a new `_is_valid_recipient()` helper (RFC-5322-shaped `local@domain.tld` regex, CR/LF header-injection block, display-name/brackets/multi-recipient/URL/path rejection, 320-char cap) and rejected with 422 before any SMTP attempt. (2) **Per-org rate limiting** — new `EmailSendLimiter` (fixed-window 3 sends / 60 min, per-org `asyncio.Lock` so concurrent sends cannot overdraw, injectable clock, constructor validation, `reset()` for tests); the route returns 429 with a `Retry-After` header when the org's budget is exhausted and fails open (logs, never blocks a legitimate send) if the limiter itself errors. **Tests** — 8 new `test_email_service.py` cases (`TestEmailSendLimiter` ×8: budget/block/rollover/per-org-isolation/concurrency/reset/constructor + `TestIsValidRecipient` ×8) + 4 new `test_admin_email.py` route tests (invalid recipient 422, email-shape matrix 422, 429+Retry-After after budget exhaustion, per-org isolation) + 4 new BDD scenarios in `email-settings.feature` (malformed recipient 422, header-injection 422, rate-limit 429 + Retry-After, no-send assertions) with 4 new step definitions. 43/43 `test_email_service.py` + 24/24 `test_admin_email.py` + 11/11 email BDD scenarios pass, ruff check + format clean, mypy --strict clean. Status: partial (no email templates/branding, concurrent PUT last-write-wins, password at rest unencrypted remain).

- 2026-08-13: improve-tests: **QA lens pass on `email_service` test package** — closed remaining branch/log gaps in `core/email_service.py` with 15 new unit tests. **Timeout edge cases:** `_effective_timeout` negative/`None` → 30s default, float truncation, lower bound 1s, upper-bound cap (120s). **Credential redaction:** `_redact_credentials` username+password, username-only, password-only, and empty-secret no-op paths asserted directly. **Logging paths:** `email.disabled_no_smtp_host` / `email.no_recipients` warnings asserted via caplog; `email.sent` verified to carry the `to`/`subject` extras; `email.send_failed` and `email.send_retry` verified to log the **redacted** error (never a raw SMTP message echoing configured credentials). **Body fallback:** HTML-only emails verified to produce a plain-text part with tags stripped (`<h1>Test</h1>` → `Test`), not an empty body. 35/35 `test_email_service.py` pass; ruff check + format clean.

- 2026-08-11: improve-architecture: **RESOLVED 3 known gaps + added BDD coverage** (`core/email_service.py`, `api/routes/admin_email.py`, `settings.py`). (1) **Configurable SMTP timeout** — `send_email` no longer hardcodes 30s; it reads `settings.smtp_timeout` via a new `_effective_timeout()` helper (missing/non-numeric/zero/oversized values clamped to the safe 1–120s range, default 30). App-level `Settings.smtp_timeout` (env-configurable, `ge=1 le=120`) added, and the admin email-settings API now accepts/echoes a per-org `smtp_timeout` (stored in `settings_json.email.smtp_timeout`, validated 1–120 → 422 outside range), wired through the test-send path so a test email honours the configured timeout. (2) **SMTP password length limit** — `smtp_password` in `EmailSettingsUpdate` is capped at 256 chars (`max_length`); oversized passwords rejected with 422 before any DB write. (3) **BDD coverage** — new `email-settings.feature` (8 scenarios: get/put with timeout, timeout 0/121 → 422, 300-char password → 422, test-send success, test-send failure with descriptive message, viewer → 403) + `test_email_settings.py` step definitions (dedicated app wired to the router with ctx-driven mocked session + `get_organisation`/`update_organisation`/`send_email` patches). **Tests:** 4 new `test_email_service.py` (custom timeout forwarded to SMTP, missing-attr/invalid/zero/huge fallback+clamp) + 5 new `test_admin_email.py` (GET timeout default + stored echo, PUT timeout persisted, timeout 0/121 → 422, password >256 → 422, test-send passes configured timeout). 22/22 `test_email_service.py` + 18/18 `test_admin_email.py` + 8/8 email BDD scenarios + settings/worker-liveness/cost-report suites pass; ruff check + format clean, mypy --strict clean. Status: partial (no email templates/branding, concurrent PUT last-write-wins, password at rest unencrypted, test-send relay abuse remain).

- 2026-08-10: improve-architecture: **RESOLVED 2 known gaps + hardened test-send route** (`core/email_service.py`, `api/routes/admin_email.py`). (1) `send_email` only caught `smtplib.SMTPException`, so network-level failures (connection refused, DNS, timeouts — all `OSError` subclasses) escaped uncaught: never retried, never wrapped in `EmailSendingError`, and the test-send route echoed the raw exception text to the client. The retry loop now catches `(smtplib.SMTPException, OSError)`, so transient network failures get up to 3 attempts and are always surfaced as `EmailSendingError`. (2) SMTP servers can echo the configured username/password in error responses; new `_redact_credentials()` scrubs both from the `EmailSendingError` message and all log extras (`email.send_retry`/`email.send_failed`). (3) The test-send route now returns a generic message for unexpected exceptions instead of echoing internal detail, and re-raises `asyncio.CancelledError`. **Tests:** 6 new unit tests in `test_email_service.py` (OSError retried+wrapped ×3 attempts, timeout retried+wrapped, transient-then-success, auth failure redacts username/password, password redacted in error message) + 3 new API tests in `test_admin_email.py` (test-send success, SMTP failure descriptive message, unexpected exception does not leak internals). 16/16 `test_email_service.py` + 13/13 `test_admin_email.py` + full error_tracking / worker_liveness / cost_report_scheduler suites pass; ruff check + format clean, mypy --strict clean. Status: partial (no BDD, timeout not configurable, no password length limit, concurrent PUT last-write-wins, password at rest unencrypted, test-send relay abuse remain).
