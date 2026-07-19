---
id: feat-observability-error-forwarders
prd: 8.25
code:
  - backend/src/modulo/api/routes/error_forwarder_config.py
bdd:
  - backend/tests/bdd/features/observability/error_forwarders.feature
unit-tests:
  - backend/tests/unit/api/test_error_forwarder_config.py
depends-on:
  - feat-observability-error-tracking
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
- [x] Sensitive config keys masked in API responses
- [x] Test result persisted to DB (last_test_at, last_test_ok)
- [x] Stale saved config used as fallback when test config is incomplete
- [x] Forwarder implementation not found returns ok=False, not an error
- [x] org_id=None returns 400 Bad Request
- [ ] Per-forwarder rate limiting
- [ ] Delivery status monitoring
- [ ] Forwarder-specific config validation (schema per type)
- [ ] Test result save rollback when forwarder succeeds but DB save fails

## Known Gaps

- **Per-forwarder rate limiting**: Each forwarder could overwhelm its target service during error spikes. No per-forwarder rate limiter exists.
- **Delivery status monitoring**: No dashboard or log of per-forwarder delivery outcomes exists beyond the `last_test_ok` field.
- **No forwarder-specific config validation**: The `config_json` dict is not validated against a per-type schema. Invalid config (missing keys, wrong types) only fails at test time.
- **Test result save race**: If the forwarder call succeeds but the DB save of `last_test_at`/`last_test_ok` fails (e.g. DB disconnect), the test event was already sent to the external service but the result is lost. The transaction rollback discards the save, not the forward.
- **_is_configured falsy edge case**: `config_json.get(k)` returns falsy for values like empty string `""` or `0`. For credential keys (dsn, api_key, etc.) this is acceptable — empty strings are equivalent to not configured — but should be documented as intentional.

## QA History

| Date | Lens | Findings | Status |
|---|---|---|---|
| 2026-07-10 | cross-cutting (6-lens) | All behaviours verified in code. Added BDD + unit tests. Documented 4 known gaps. Updated depends-on. | ✅ |
