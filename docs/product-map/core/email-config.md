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

## Known Gaps

- **No BDD coverage** — No `.feature` files for email configuration (GET/PUT settings, test email, password clearing). Should be added under `tests/bdd/features/notifications/email.feature`.
- **Email templates not implemented** — No per-organisation email template system. Both unchecked behaviours require a template rendering pipeline (stored templates, variable substitution, HTML inlining).
- **Per-organisation email branding not implemented** — No support for org-level from-name, logo, or footer customisation in outgoing emails.
