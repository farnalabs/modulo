# Troubleshooting Guide

Common issues, their causes, and resolutions.

---

## 1. Startup Failures

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| `SECRET_KEY not set` | Missing env var | Set `SECRET_KEY` (minimum 32 bytes) | `RuntimeError: SECRET_KEY is not set` |
| `FERNET_KEY not set` | Missing env var | Set `FERNET_KEY` (base64-encoded, 32 bytes) | `RuntimeError: FERNET_KEY is not set` |
| `Cannot connect to Postgres` | DB not running, wrong `DATABASE_URL`, or network issue | Check `docker compose ps`, verify `DATABASE_URL` is correct, ensure Postgres is accepting connections | `sqlalchemy.exc.OperationalError: could not connect to server` |
| `Alembic migration failed` | Version mismatch, branch migration, or `VARCHAR(32)` column width | Check `alembic_version` table exists with `VARCHAR(255)` for branch IDs; run `uv run alembic upgrade heads` | `alembic.util.exc.CommandError` or `psycopg2.errors.StringDataRightTruncationError` |
| `Redis connection refused` | Redis not running or wrong `REDIS_URL` | Check `docker compose ps`, verify `REDIS_URL` | `redis.exceptions.ConnectionError: Error 10061` |
| `Address already in use` | Port conflict (another process on the same port) | Change port via env vars or kill conflicting process (`netstat -ano \| findstr :PORT`) | `OSError: [Errno 10048] error while attempting to bind on address` |

---

## 2. Authentication Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| `401 Unauthorized` on every request | Invalid, expired, or malformed JWT | Re-login with `POST /api/v1/auth/login` to obtain a fresh token | `401: Token has expired` or `401: Invalid token` |
| `403 Forbidden` | User role lacks required permission | Check user role (`admin`/`operator`/`runner`); upgrade role via admin API if needed | `403: Insufficient permissions` |
| Login succeeds but no data returned | No organisation has been created | Create an org via the admin API or run the seed script (`uv run scripts/seed.py`) | No error — empty responses from all API calls |
| `Invalid API key` | Wrong key, expired, or revoked | Create a new API key in admin settings; verify the key prefix matches the expected pattern | `401: Invalid API key` |
| SSO login redirect fails | OIDC/SAML provider misconfiguration | Check provider settings (client ID, client secret, discovery URL); verify `redirect_uri` matches the provider's allowlist | `OIDCError: redirect_uri_mismatch` |

---

## 3. Pipeline Execution Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Run stuck in `running` status | Node timeout, LLM not responding, or cancelled node never fires | Check `@cancellable_node` timeout configuration; verify model backend is configured and reachable | `Node <name> exceeded timeout of <N>s` |
| Run fails with `LLM_TIMEOUT` | Model backend not responding within configured timeout | Check model backend credentials and connectivity; increase timeout in agent settings | `LLM_TIMEOUT: Model backend <name> did not respond within <N>s` |
| Run fails with `CONNECTOR_ERROR` | Connector auth failure or network issue | Check connector credentials in settings; verify the connector's target service is accessible from the backend host | `CONNECTOR_ERROR: <connector_name>: <error_details>` |
| Run fails with `VALIDATION_ERROR` | Agent output doesn't match expected schema | Check agent output against the assigned schema; fix prompt to produce conformant output | `VALIDATION_ERROR: Schema <name>: <validation_errors>` |
| `Graph validation failed` | Pipeline topology is invalid | Check edge connections for compatibility (node types, schema matching) | `GraphValidationError: <reason>` |
| `Max concurrent runs exceeded` | Too many runs in progress simultaneously | Wait for active runs to complete; increase the concurrent run limit via admin settings | `429: Max concurrent runs exceeded (limit: <N>)` |

---

## 4. HITL Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Gate not appearing in UI | Pipeline has `human_only` flag set, preventing automatic show | Check `human_only` flag on the pipeline — these gates require explicit human review and won't auto-proceed | Pipeline remains in `awaiting_human` state |
| `Claim token expired` | 15-minute TTL exceeded since claim | Re-claim the gate — a new claim token is auto-generated | `401: Claim token expired` |
| `409 Conflict on claim` | Another user already claimed this gate | Wait for the other user to complete their review or for the claim token to expire (15 min) | `409: Gate <id> already claimed by user <name>` |
| Cannot approve/reject | Claim token is invalid, expired, or gate already decided | Refresh the page; re-claim the gate if needed | `401: Invalid claim token` or `409: Gate already decided` |
| `human_only gate blocked` | Pipeline has `human_only: true` and requires a human-in-the-loop | This is by design — use the UI or MCP `review_hitl` tool to review; auto-approval is not possible | `human_only gate <id> is blocked — manual review required` |

---

## 5. WebSocket/Event Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| WebSocket disconnects frequently | Network issues, proxy timeout, or load balancer idle timeout | Check reverse proxy WebSocket support (e.g. `proxy_read_timeout` in nginx); increase timeout configuration | `WebSocket disconnected: code 1006` |
| Events not updating in UI | WebSocket disconnected or event broker ring buffer full | Refresh the page; reconnect will replay from the last event sequence number | Missing live updates on stage board or run inspection |
| Replay events not working | Requested `since_event_seq` is outside the 100-event ring buffer range | Use a lower sequence number or perform a full reconnect (omit `since_event_seq`) | `400: since_event_seq <N> not available (buffer: <min>-<max>)` |

---

## 6. Webhook Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Webhook not firing | Endpoint auto-disabled after repeated failures | Re-enable the endpoint in notification settings; check endpoint availability | `Endpoint <url> disabled after <N> consecutive failures` |
| HMAC validation failing | HMAC secret mismatch between sender and receiver | Rotate the HMAC secret in notification settings and update the receiver | `HMAC signature mismatch` |
| Duplicate webhook calls | Retry mechanism delivering the same event multiple times | Check the delivery log for retry count; verify the endpoint handles idempotency via the `X-Modulo-Delivery-Id` header | Multiple delivery log entries with the same `delivery_id` |
| `Flood protection triggered` | Too many identical webhooks in a short window | Check deduplication configuration; verify the webhook source is not sending duplicate payloads | `429: Flood protection — too many identical webhooks` |

---

## 7. Performance Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Slow pipeline execution | LLM latency, connector latency, or resource contention | Check p50/p95/p99 duration in run stats; review model backend health; consider switching to a faster model | High `duration_ms` in run inspection |
| High memory usage | Too many runs held in memory simultaneously | Reduce `max_concurrent_runs`; review checkpoint cleanup settings | OOM-killer events or rising RSS in container metrics |
| Slow API responses | DB query performance or missing indexes | Check slow query log; review index strategy (add missing indexes on frequently-queried columns) | Queries exceeding 100ms in slow query log |
| UI feels sluggish | Large pipeline graphs, excessive WebSocket events, or browser memory pressure | Reduce pipeline complexity (fewer nodes/edges); check browser console for JS errors or memory warnings | Browser DevTools Performance tab showing long frame times |

---

## 8. Known Limitations

- **SQLite mode**: No RLS enforcement, no advisory locks, no flood protection. Development only — not for production.
- **Claim tokens**: Single-use with a 15-minute TTL. Expired tokens cannot be refreshed — re-claim the gate.
- **WebSocket ring buffer**: Limited to 100 events per run. Older events are not available for reconnect replay.
- **Postgres required for production**: SQLite is development-only. Postgres is the only supported production database. See [`docs/system-requirements.md`](./system-requirements.md).
- **File upload limit**: Webhook payloads are limited to 10 MB.
- **Concurrent runs**: Hard limit enforced by `max_concurrent_runs` config. Excess runs receive a 429 response.
- **API key scoping**: Keys are scoped to `operator` and `runner` roles only. Admin operations require JWT auth.

---

## Log Locations

| Environment | Log Source | Location |
|---|---|---|
| Docker (local) | Backend stdout | `docker compose logs -f modulo-api` |
| Docker (local) | Postgres | `docker compose logs -f db-local` |
| Docker (local) | Redis | `docker compose logs -f redis-local` |
| Production | Backend (JSON structured) | `journalctl` or log file per deployment config |
| Production | Postgres slow query log | `postgresql-<date>.log` (configurable via `log_min_duration_statement`) |
| Production | Nginx/Igress | Access and error logs per ingress controller |
