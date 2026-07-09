---
id: feat-core-email-config
prd: 8.11
code:
  - backend/src/modulo/api/routes/admin_email.py
bdd: []
unit-tests: []
depends-on: []
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
