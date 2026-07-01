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

status: partial
---
# API Rate Limiting

## Behaviours

### Auth rate limiting (6.10 cross-ref)
- [ ] Login endpoint: 10 failed attempts per IP per minute returns 429
- [ ] Exponential backoff applied after rate limit exceeded on login
- [ ] Counter resets after successful login

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
- [ ] Admin API: GET returns rules and mode
- [ ] Admin API: PUT updates rules dynamically
- [ ] Admin API: non-admin gets 403

### Edge cases
- [x] Missing `X-Forwarded-For` header falls back to `request.client.host` (rate_limiter.py:128)
- [x] Unknown client host falls back to `"unknown"` (rate_limiter.py:128)
- [x] Empty bypass token header treated as no token (rate_limiter.py:112-113: empty str is falsy)
- [x] Empty `modulo_ratelimit_bypass_token` setting disables bypass entirely (rate_limiter.py:113: both token and bypass must be non-empty)
- [x] Redis connection failure falls back gracefully to in-memory (rate_limiter.py:44-47: exception caught, falls to in-memory)
- [ ] Negative or zero `max_requests` rejected at schema level (Field(gt=0))
- [ ] `window_s` less than 1 rejected at schema level (Field(ge=1))
- [ ] Runtime rules update does not reset existing in-flight counters
- [ ] At least one rule required on PUT (400 if empty)
- [x] Path prefix matching: `/api/v1/runs` matches both exact and sub-routes (rate_limiter.py:121: `path.startswith(prefix)`)
- [x] TokenBucket thread-safe via `asyncio.Lock`

## Known Gaps
- BDD feature file at `backend/tests/bdd/features/model_backends/rate_limiting.feature` is a placeholder — no real scenarios
- No BDD coverage for any rate limit endpoint or behaviour
- No integration/E2E test that exercises Redis sliding window against a real Redis
- MCP-specific rate limit rules (`trigger_pipeline` vs general MCP calls) are not differentiated in middleware — all `/mcp` paths share 200 req/min rule (trigger_pipeline has a separate 60/min limit at the application level in `mcp_server.py`)
- HITL review rate limit (20/min per PRD §7.18) is not enforced as a separate rule — `/api/v1/runs` catch-all covers HITL paths at 60/min instead of the specified 20/min 
- **MCP trigger_pipeline 60/min rate limit NOT implemented**: The product map entry incorrectly claimed `[x]` — there is no TokenBucket or application-level rate limiting for trigger_pipeline. Only the generic `/mcp` 200/min middleware limit applies.
- **BDD feature file in wrong directory**: The file lives at `backend/tests/bdd/features/model_backends/rate_limiting.feature` under `model_backends/` instead of a proper `rate_limiting/` directory.
- **In-memory TokenBucket fallback uses hardcoded defaults**: `RateLimiterRegistry` creates TokenBuckets with `rate=10.0, burst=20` regardless of the configured rule — when Redis is unavailable, the rate limit enforcement uses these defaults instead of the actual rule's max_requests/window_s.
- **HITL review rate limit (20/min per PRD §7.18) not separately enforced**: Covered only by the `/api/v1/runs` catch-all at 60/min.
- **No integration/E2E test**: No test exercises Redis sliding window against an actual Redis instance.