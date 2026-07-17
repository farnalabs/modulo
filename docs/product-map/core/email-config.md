---
id: feat-core-email-config
prd: 8.11
code:
  - backend/src/modulo/api/routes/admin_email.py
  - backend/src/modulo/core/email_service.py
bdd: []
unit-tests:
  - backend/tests/unit/api/test_admin_email.py
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
- [ ] Email templates configuration
- [ ] Per-organisation email branding

## Error Handling

- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] SMTP not configured returns 422 on test-send
- [x] Invalid SMTP host/port returns 422 validation error
- [x] SMTP connection failure during test returns descriptive error with failure reason
- [x] Password clear when no password stored returns 200 (idempotent)
- [ ] SMTP auth failure during test — may leak credentials in error message

## Edge Cases

- [x] Empty SMTP host returns 422
- [x] Invalid port number returns 422
- [x] Missing email settings returns 501 (ProgrammingError for missing DB table)
- [ ] SMTP server unreachable during test — timeout not configurable
- [ ] Large SMTP password — no length limit enforced
- [ ] Concurrent PUT of email settings — last-write-wins on `settings_json`

## Security

- [x] Admin-only access (org admin role required)
- [x] SMTP password stored in settings_json — masked in GET responses
- [ ] No encryption for SMTP password at rest (stored in plain JSON column)
- [ ] Test-send endpoint could be abused for SMTP relay enumeration

## Known Gaps
