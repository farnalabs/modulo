---
id: feat-observability-error-forwarders
prd: 8
code:
  - backend/src/modulo/api/routes/error_forwarder_config.py
bdd: []
unit-tests: []
depends-on: []
status: partial
---

# Error Forwarders

Configuration and management of error forwarding destinations (Sentry, DataDog, PagerDuty, Rollbar, OpsGenie, Loki). Admins can list, configure, enable/disable, and test forwarder connections.

## Behaviours

- [x] List all available forwarders with enabled/configured status
- [x] Configure a forwarder with type-specific credentials
- [x] Test forwarder connection with a synthetic error event
- [x] Enable/disable forwarders independently
- [x] Gated behind `error_forwarders` feature flag
- [x] Admin role required for modification
- [x] Forwarder type validation (404 on unknown)
- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] Test connection timeout at 15s
- [ ] Per-forwarder rate limiting
- [ ] Delivery status monitoring
