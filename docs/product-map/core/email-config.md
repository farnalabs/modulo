---
id: feat-core-email-config
prd: 8.11
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/admin_email.py
  - backend/src/modulo/core/email_service.py
bdd: []
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

## Edge Cases

- [x] Empty SMTP host returns 422
- [x] Invalid port number returns 422
- [x] Missing email settings returns 501 (ProgrammingError for missing DB table)
- [x] SMTP auth failure redacts the configured username/password from the returned message (was leaking credentials in error message)
- [x] Network-level failures (connection refused, timeout) are retried up to 3 attempts before raising `EmailSendingError` (was escaping uncaught as raw `OSError`)
- [ ] SMTP server unreachable during test — timeout not configurable (fixed value 30s)
- [ ] Large SMTP password — no length limit enforced
- [ ] Concurrent PUT of email settings — last-write-wins on `settings_json`

## Security

- [x] Admin-only access (org admin role required)
- [x] SMTP password stored in settings_json — masked in GET responses
- [x] SMTP credentials redacted from SMTP error messages before reaching logs or clients
- [ ] No encryption for SMTP password at rest (stored in plain JSON column)
- [ ] Test-send endpoint could be abused for SMTP relay enumeration

## Known Gaps

- **No BDD coverage** — no `.feature` file for the email settings endpoints.
- **Timeout not configurable** — `send_email` hardcodes a 30-second SMTP timeout; no setting to override it.
- **No password length limit** — large SMTP passwords are stored as-is without validation.
- **Concurrent PUT is last-write-wins** — no optimistic locking on `settings_json` for email settings.
- **SMTP password at rest** — stored in the plain `settings_json` JSON column; no encryption.
- **Test-send relay abuse** — the endpoint sends mail to an arbitrary recipient address; no rate limiting or recipient allowlisting.

## QA History

- 2026-08-10: improve-architecture: **RESOLVED 2 known gaps + hardened test-send route** (`core/email_service.py`, `api/routes/admin_email.py`). (1) `send_email` only caught `smtplib.SMTPException`, so network-level failures (connection refused, DNS, timeouts — all `OSError` subclasses) escaped uncaught: never retried, never wrapped in `EmailSendingError`, and the test-send route echoed the raw exception text to the client. The retry loop now catches `(smtplib.SMTPException, OSError)`, so transient network failures get up to 3 attempts and are always surfaced as `EmailSendingError`. (2) SMTP servers can echo the configured username/password in error responses; new `_redact_credentials()` scrubs both from the `EmailSendingError` message and all log extras (`email.send_retry`/`email.send_failed`). (3) The test-send route now returns a generic message for unexpected exceptions instead of echoing internal detail, and re-raises `asyncio.CancelledError`. **Tests:** 6 new unit tests in `test_email_service.py` (OSError retried+wrapped ×3 attempts, timeout retried+wrapped, transient-then-success, auth failure redacts username/password, password redacted in error message) + 3 new API tests in `test_admin_email.py` (test-send success, SMTP failure descriptive message, unexpected exception does not leak internals). 16/16 `test_email_service.py` + 13/13 `test_admin_email.py` + full error_tracking / worker_liveness / cost_report_scheduler suites pass; ruff check + format clean, mypy --strict clean. Status: partial (no BDD, timeout not configurable, no password length limit, concurrent PUT last-write-wins, password at rest unencrypted, test-send relay abuse remain).
