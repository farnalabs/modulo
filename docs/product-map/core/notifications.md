---
id: feat-core-notifications
prd: 8.11
delivery-tasks: [task-nv1-team-notifications]
bdd:
  - backend/tests/features/notifications/hitl_webhook.feature
  - backend/tests/features/notifications/failure_webhook.feature
  - backend/tests/features/notifications/signing.feature
  - backend/tests/features/hitl/overdue_warning.feature
unit-tests: []
code:
  - backend/src/modulo/core/notifier/__init__.py
  - backend/src/modulo/api/routes/admin_notifications.py
  - backend/src/modulo/db/models/notification_endpoint.py
  - backend/src/modulo/db/models/notification_delivery.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
depends-on: [feat-teams-team-crud]
status: partial
---

# Core Notifications

Outbound webhook notifications for pipeline lifecycle events, with HMAC signing, retry, dead-letter tracking, and auto-disable.

## Behaviours

### Event dispatch

- [x] `hitl_awaiting` event dispatches when a run reaches a HITL gate
- [x] `run_failed` event dispatches when a pipeline node raises an unhandled exception
- [x] `claim_expired` event dispatches when a HITL claim expires (via ClaimExpiryJob) — Notifier.dispatch_event called in ClaimExpiryJob._expire_once
- [ ] `hitl_overdue` event dispatches when a HITL gate passes its configurable threshold — event type constant exists but no background job dispatches it
- [ ] `budget_exceeded` event type is defined in PRD §8.11 but not yet dispatched — event type constant does not exist in code
- [ ] `circuit_breaker_tripped` event type is defined in PRD §8.11 but not yet dispatched — event type constant does not exist in code
- [x] Webhook POST body includes event type, ISO timestamp, and event-specific payload
- [x] Payload includes `run_id` and `gate_id` for HITL-related events
- [x] Payload includes `pipeline_name` and `node_name` for gate events
- [x] Payload includes `error_code` and `error_message` for failure events
- [ ] `Notifier.dispatch_event` wraps main dispatch body in try/except — unexpected errors in `_get_subscribed_endpoints` or per-endpoint dispatch propagate unhandled
- [ ] `Notifier._record_delivery` wraps session operations in try/except — DB failure on delivery recording propagates unhandled

### HMAC signing

- [x] Outbound webhook includes `X-Modulo-Signature: sha256=<hmac>` header
- [x] Signature is HMAC-SHA256 over the JSON payload body
- [x] Signature uses per-endpoint secret, not a global secret
- [x] Endpoint with no secret configured returns empty signature (no header)
- [x] Endpoint with corrupted secret ciphertext returns empty signature (logged, not crashed)
- [x] Secrets stored as Fernet ciphertext in database (never plaintext)
- [x] Secrets never exposed in API responses (only `has_secret: bool`)### Retry and dead-letter
- [x] 4xx (non-429) or network error triggers retry — any non-2xx response or RequestError triggers retry with fixed delays [5s, 30s, 120s]
- [x] 5xx response triggers retry with same delay schedule as 4xx
- [ ] 429 response uses `Retry-After` header (capped at 60s) — code does not inspect Retry-After header
- [x] Retry delays: [5.0s, 30.0s, 120.0s] up to MAX_RETRIES (3) — defined as module-level constants in notifier/__init__.py
- [x] After MAX_RETRIES exhausted, status is set to "dead_lettered" — line 278 in _dispatch_to_endpoint
- [x] Dead-lettered events log to `notification_delivery_log` with attempt count and last error — _record_delivery always called
- [x] Consecutive dead-letter counter incremented on each failure — atomic UPDATE...RETURNING in _increment_dead_letter
- [x] Successful delivery resets consecutive dead-letter counter to 0 — _reset_dead_letter with guard `consecutive_dead_letter_count > 0`
- [x] Endpoint auto-disabled when consecutive_dead_letter_count >= MAX_DEAD_LETTERS (10) — auto_disabled=True + disabled_at set in _increment_dead_letter
- [x] Auto-disabled endpoint stores `disabled_at` timestamp
- [x] Auto-disabled endpoints are skipped in dispatch queries — `auto_disabled.is_(False)` in _get_subscribed_endpoints WHERE clause
- [ ] PRD §8.11 specifies 5 consecutive failures within 24h triggers auto-disable; code uses 10 consecutive with no time window — documented gap

### Delivery log

- [x] Every dispatch attempt recorded in `notification_delivery_log` — _record_delivery called after every dispatch attempt
- [x] Delivery log stores event_type, endpoint_id, run_id, status, attempt_count, response_code, last_error — via NotificationDeliveryLog model
- [ ] Failed deliveries for `hitl_awaiting` trigger in-app alert to org admins — no in-app alert mechanism exists yet
- [x] Payload ciphertext stored in delivery log when `retain_payload=True` — Fernet-encrypted in _dispatch_to_endpoint
- [x] Cursor-based pagination on delivery log endpoints — /deliveries and /{id}/deliveries support cursor (ISO datetime-based)
- [x] Filtering by status, endpoint_id, date range on delivery log — /deliveries supports status, endpoint_id, event_type, date_from, date_to

### Admin API (CRUD)

- [x] Admin can create webhook endpoint with URL, optional secret, event subscription list, and description — POST /webhooks
- [x] Creating endpoint with non-http/https URL returns 422 — field_validator on WebhookCreate.url
- [x] Creating endpoint with unknown event type returns 422 — field_validator checks against AVAILABLE_EVENTS
- [x] Admin can list all endpoints for their org — GET /webhooks org-scoped via RLS
- [x] Admin can get a single endpoint by ID — GET /webhooks/{id} with 404 if not found or wrong org
- [x] Admin can update endpoint URL, secret, events, description — PUT /webhooks/{id} updates each field independently
- [x] Admin can delete endpoint — DELETE /webhooks/{id} returns 204
- [x] Admin can test endpoint (sends ping payload with signature) — POST /webhooks/{id}/test
- [x] Admin can re-enable auto-disabled endpoint (resets disabled flag and counter) — POST /webhooks/{id}/re-enable
- [x] Admin can manually retry a failed delivery — POST /webhooks/{id}/deliveries/{delivery_id}/retry
- [x] Non-admin role gets 403 on all notification endpoints — _require_admin checks principal.org_role
- [x] RLS enforces org-scoped isolation on all queries — set_rls_org called per-transaction
- [x] Endpoint not found returns 404 across all operations — org_id cross-check on GET/PUT/DELETE/test/re-enable
- [ ] All admin notification routes catch `sqlalchemy.exc.ProgrammingError` and return 501 Not Implemented — list_webhooks, create_webhook, get_webhook, update_webhook, delete_webhook, test_webhook, re_enable_webhook, list_deliveries, retry_delivery, retry_all_failed_deliveries, list_available_events lack the catch (only list_all_deliveries has it)

### Team-scoped dispatch

- [x] When `team_id` is provided, dispatch routes to team-specific endpoints — implemented in _get_subscribed_endpoints (line 182-196)
- [x] When team has no endpoints, falls back to org-wide endpoints — falls back to `team_id IS NULL` query
- [x] When `team_id` is None, only org-wide endpoints are returned — queries `team_id.is_(None)`
- [x] Org-wide dispatch excludes endpoints with a team_id — query condition `team_id.is_(None)`
- [ ] Team notification endpoint configuration via admin API is v1 per PRD — code supports team_id on NotificationEndpoint model but create/update routes have no team_id field

### Claim expiry background job

- [x] Expiry job polls every 60s for expired HITL claims — POLL_INTERVAL = 60.0 in expiry_job.py
- [x] Expired claims reset: claimed_by=null, claim_token=null, expires_at=null, claimed_at=null — batch UPDATE in _expire_once
- [x] Affected runs transition from "claimed" to "awaiting_human" — UPDATE Run SET status='awaiting_human' WHERE status='claimed'
- [x] `claim_expired` notification dispatched per expired claim — Notifier.dispatch_event called for each expired entry in _expire_once
- [x] Job runs per-org with RLS scoping to avoid cross-org leakage — iterates orgs, sets RLS per transaction
- [x] Job errors logged and caught (one org failure does not crash the loop) — outer try/except in _run, inner try/except per notification dispatch
- [x] Job cancels cleanly on application shutdown — _stop_event + task.cancel() pattern

### Security

- [x] All admin notification routes require admin role — _require_admin guard on every route
- [x] RLS applied per-transaction with `set_rls_org`
- [x] URL validated to be absolute http/https — field_validator on WebhookCreate/WebhookUpdate
- [x] Secrets encrypted at rest with Fernet — Fernet(fernet_key) in create/update routes
- [x] Payloads optionally encrypted in delivery log — Fernet.encrypt when retain_payload=True

### Concurrency

- [x] Dispatch to each endpoint is sequential (no concurrent deliveries to same endpoint in one call) — `for ep in endpoints: await self._dispatch_to_endpoint(...)`
- [x] Multiple endpoints in one dispatch_event are processed sequentially
- [x] Claim expiry job runs as asyncio task (not Celery) in alpha — asyncio.create_task in ClaimExpiryJob.start
- [ ] Multi-worker advisory lock for expiry job specified in PRD §8.11 but not yet implemented — documented gap

### Backward compatibility

- [x] Empty events list on endpoint treated as no subscription (valid, no-op) — json.loads returns [] which fails event_type in [] check
- [x] Malformed events JSON is skipped (not crashed) — try/except json.JSONDecodeError, TypeError in _get_subscribed_endpoints
- [x] Null secret_ciphertext produces empty signature — _sign_payload returns "" when endpoint.secret_ciphertext is None
- [x] Null team_id produces org-wide dispatch behaviour — _get_subscribed_endpoints queries team_id.is_(None)
- [x] MAX_RETRIES, MAX_DEAD_LETTERS, RETRY_DELAYS are module-level constants (configurable)

## Known Gaps
- `budget_exceeded` and `circuit_breaker_tripped` event types from PRD §8.11 not yet dispatched
- Slack native integration listed in PRD as v1 — not implemented
- PRD §8.11 specifies 5 consecutive failures within 24h for auto-disable; code uses 10 consecutive with no time window
- `X-Modulo-Timestamp` header referenced in signing.feature but not emitted by notifier code — replay protection gap
- Celery-based dispatcher isolation (PRD v1) — dispatcher still runs in FastAPI process
- Multi-worker advisory lock for expiry job not yet implemented
- `signing.feature` references header `X-Modulo-Signature-256` but code emits `X-Modulo-Signature` — BDD mismatch
- `signing.feature` Scenario "Signature uses different secrets per org" describes per-org secrets; code uses per-endpoint secrets — BDD must describe endpoint-level secrets
- `failure_webhook.feature` Scenario "Endpoint auto-disabled after repeated failures" says 5 consecutive failures; code uses MAX_DEAD_LETTERS=10 — BDD mismatch
- `hitl_awaiting` failed deliveries do not trigger in-app alerts to org admins — no alert mechanism exists
- Team notification endpoint configuration (team_id field) not exposed in admin API create/update routes — NotificationEndpoint model has team_id column but API does not surface it
- `hitl_overdue` event type constant exists in AVAILABLE_EVENTS but no background job dispatches it
- 429 Retry-After header not honored — all retries use fixed delay schedule
- `Notifier.dispatch_event` lacks top-level try/except — unexpected errors propagate to caller
- 9 of 11 admin notification routes lack `sqlalchemy.exc.ProgrammingError` catch (501 Not Implemented fallback) — only `/deliveries` (list_all_deliveries) has it