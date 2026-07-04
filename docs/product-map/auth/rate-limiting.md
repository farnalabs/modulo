---
id: feat-auth-rate-limiting
prd: 7.18
delivery-tasks: [task-nv12-rate-limiting]
bdd:
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
code:
  - backend/src/modulo/api/middleware/rate_limiter.py
  - backend/src/modulo/core/rate_limiter.py
  - backend/src/modulo/api/routes/admin_rate_limits.py
unit-tests:
  - backend/tests/unit/rate_limiter/test_rate_limiter.py
  - backend/tests/unit/api/test_rate_limiting_bdd.py
  - backend/tests/unit/api/test_rate_limiter_middleware.py
  - backend/tests/unit/api/test_rate_limiter_keys.py
  - backend/tests/bdd/steps/test_rate_limiting.py
depends-on: [feat-auth-jwt-auth]
status: partial
---
# API Rate Limiting

Redis-backed sliding window and in-memory token bucket rate limiting for POST/PUT/PATCH endpoints, MCP tools, and login auth, with per-endpoint configurable rules and bypass token support.

## Behaviours

### POST /api/v1/runs
- [x] 60 requests per minute per API key (middleware rule matches PRD §7.18)
- [x] Returns 429 with `retry-after` header when exceeded
- [x] Response body includes `error_code: rate_limit_exceeded`
- [x] GET requests to the same path are not rate limited
- [x] PUT/PATCH requests are rate limited

### POST /api/v1/webhooks/&lt;trigger_id&gt;
- [x] 100 requests per minute per trigger (middleware rule matches PRD §7.18)
- [x] Returns 429 when exceeded
- [x] Exceeded requests logged as `TriggerEvent` with `rate_limited` status
- [ ] Webhook flood protection separate from per-trigger rate limit

### MCP trigger_pipeline tool
- [ ] 60 calls per minute per MCP client ID (app-level TokenBucket with rate=1.0, burst=60)
- [x] Returns error response when exceeded (returns `{"error":"rate_limited",...}`)

### POST /runs/{id}/hitl/{gate_id}/review
- [ ] 20 requests per minute per user (not yet enforced as separate rule — covered by runs catch-all at 60/min)
- [ ] Returns 429 when exceeded

### Any MCP tool call
- [x] 200 requests per minute per MCP client ID (middleware rule matches PRD §7.18)
- [x] Returns 429 when exceeded

### Backend implementation
- [x] Redis-backed sliding window (ZADD + ZREMRANGEBYSCORE) as primary
- [x] In-memory token bucket fallback when Redis unavailable
- [x] Startup warning logged when running in-memory mode
- [x] Rate limiting disabled entirely in SQLite mode (in-memory only, no Redis connection)
- [x] Rate limit rules configurable at runtime via `PUT /api/v1/admin/rate-limits`
- [x] Only admin users can read/update rate limit rules
- [x] Bypass token (`MODULO_RATELIMIT_BYPASS_TOKEN`) skips rate limiting
- [x] Client keyed by `X-Forwarded-For` IP + request path
- [x] Only POST/PUT/PATCH methods are rate limited
- [x] `Retry-After` header set to the window duration in seconds

### Concurrency & multi-worker
- [x] Redis coordinates rate limit state across multiple worker processes
- [ ] In-memory fallback is per-process — effectively doubles limit on N replicas
- [ ] `Retry-After` response returned before the request handler runs (middleware order)

### Unit test coverage
- [x] TokenBucket: consume when tokens available
- [x] TokenBucket: blocks when empty, refills over time
- [x] TokenBucket: burst ceiling enforced
- [x] RedisSlidingWindowRateLimiter: allows within limit
- [x] RedisSlidingWindowRateLimiter: blocks over limit
- [x] RedisSlidingWindowRateLimiter: exact boundary at limit
- [x] RedisSlidingWindowRateLimiter: custom key prefix
- [x] RedisSlidingWindowRateLimiter: custom window duration
- [x] RateLimiterRegistry: in-memory fallback by default
- [x] RateLimiterRegistry: uses Redis when available
- [x] RateLimiterRegistry: Redis blocks over limit
- [x] Middleware: accepts explicit settings injection
- [x] Middleware: accepts explicit registry injection
- [x] Middleware: valid bypass token skips rate limiting
- [x] Middleware: invalid bypass token does not skip
- [x] Middleware: returns 429 with `rate_limit_exceeded` error code
- [x] Middleware: GET requests not rate limited
- [x] Middleware: MCP paths rate limited
- [x] Admin API: GET returns rules and mode
- [x] Admin API: PUT updates rules dynamically
- [x] Admin API: non-admin gets 403

### Edge cases
- [x] Missing `X-Forwarded-For` header falls back to `request.client.host` (rate_limiter.py:128)
- [x] Unknown client host falls back to `"unknown"` (rate_limiter.py:128)
- [x] Empty bypass token header treated as no token (rate_limiter.py:112-113: empty str is falsy)
- [x] Empty `modulo_ratelimit_bypass_token` setting disables bypass entirely (rate_limiter.py:113: both token and bypass must be non-empty)
- [x] Redis connection failure falls back gracefully to in-memory (rate_limiter.py:44-47: exception caught, falls to in-memory)
- [x] Negative or zero `max_requests` rejected at schema level (Field(gt=0))
- [x] `window_s` less than 1 rejected at schema level (Field(ge=1))
- [x] At least one rule required on PUT (400 if empty)
- [x] Path prefix matching: `/api/v1/runs` matches both exact and sub-routes (rate_limiter.py:121: `path.startswith(prefix)`)
- [x] TokenBucket thread-safe via `asyncio.Lock`

### Error handling
- [x] Middleware catches Redis connection failure and falls back to in-memory (rate_limiter.py:50-51)
- [x] Auth rate limiter catches Redis connection failure and falls back to in-memory (rate_limiter.py:203-204)
- [x] Invalid/malformed JWT falls back to IP-based rate limiting (rate_limiter.py:157-158)
- [x] Missing X-Forwarded-For falls back to request.client.host (rate_limiter.py:161-162)
- [x] Unknown client host falls back to "unknown" (rate_limiter.py:162)
- [x] Empty bypass token treated as no bypass (rate_limiter.py:117-118: `if token and self._bypass_token and token == self._bypass_token`)
- [x] shutdown_rate_limiters() closes all Redis clients gracefully (rate_limiter.py:265-278)

### Auth rate limiting (§6.10)
- [x] Login endpoint: 10 failed attempts per IP per minute returns 429 (AuthRateLimitMiddleware, AuthRateLimiter.check_login)
- [x] Exponential backoff: tiered backoff = min(2^(tier-1) * 60, 3600) seconds (AuthRateLimiter._compute_backoff)
- [x] Counter resets after successful login (AuthRateLimiter.record_success)
- [x] In-memory fallback when Redis unavailable (get_auth_rate_limiter lines 203-204)
- [x] Configurable via modulo_auth_max_attempts, modulo_auth_window_seconds settings

## Known Gaps
- BDD feature file at `backend/tests/bdd/features/model_backends/rate_limiting.feature` — 11 real scenarios written covering PRD §7.18 endpoints. Step definition path was fixed in this QA iteration (2026-07-04: path mismatch resolved, step definitions now load correctly).
- No unit-test-level step definitions for the BDD scenarios (unit tests exist via `test_rate_limiting_bdd.py` but are not wired as BDD step definitions)
- No integration/E2E test that exercises Redis sliding window against a real Redis
- MCP-specific rate limit rules (`trigger_pipeline` vs general MCP calls) are not differentiated in middleware — all `/mcp` paths share 200 req/min rule (trigger_pipeline has a separate 60/min limit at the application level in `mcp_server.py`)
- HITL review rate limit (20/min per PRD §7.18) is not enforced as a separate rule — `/api/v1/runs` catch-all covers HITL paths at 60/min instead of the specified 20/min
- Auth rate limiting (§6.10) has no unit tests for AuthRateLimiter or AuthRateLimitMiddleware classes directly.
## QA History
- 2026-07-04: Cross-cutting QA (index 153). Fixed 2 pre-existing test bugs (response format mismatch with ProblemDetail RFC 9457). Fixed BDD step definitions path. Marked 9 stale [ ]→[x] behaviour checkboxes. Added Error Handling section (7 checkboxes). Added Auth Rate Limiting section (5 checkboxes). Consolidated duplicate Known Gaps. All 59 tests pass.
- 2026-07-05: QA-iterate (prodmap auth). Fixed TokenBucket `rate`/`burst` computation to use configured params instead of hardcoded defaults. Moved FIXED item from Known Gaps to QA History.