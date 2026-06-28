---
id: feat-auth-rate-limiting
prd: §7.18
delivery-tasks: [task-nv12-rate-limiting]
bdd:
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
code:
  - backend/src/modulo/api/middleware/rate_limiter.py
  - backend/src/modulo/core/rate_limiter.py
  - backend/src/modulo/api/routes/admin_rate_limits.py
depends-on: []
status: partial
---

# API Rate Limiting

## Behaviours

### Auth rate limiting (§6.10 cross-ref)
- [ ] Login endpoint: 10 failed attempts per IP per minute returns 429
- [ ] Exponential backoff applied after rate limit exceeded on login
- [ ] Counter resets after successful login

### POST /api/v1/runs
- [ ] 60 requests per minute per API key
- [ ] Returns 429 with `retry-after` header when exceeded
- [ ] Response body includes `error_code: rate_limit_exceeded`
- [ ] GET requests to the same path are not rate limited
- [ ] PUT/PATCH requests are rate limited

### POST /api/v1/webhooks/<trigger_id>
- [ ] 100 requests per minute per trigger
- [ ] Returns 429 when exceeded
- [ ] Exceeded requests logged as `TriggerEvent` with `rate_limited` status
- [ ] Webhook flood protection separate from per-trigger rate limit

### MCP trigger_pipeline tool
- [ ] 60 calls per minute per MCP client ID
- [ ] Returns 429 when exceeded

### POST /runs/{id}/hitl/{gate_id}/review
- [ ] 20 requests per minute per user
- [ ] Returns 429 when exceeded

### Any MCP tool call
- [ ] 200 requests per minute per MCP client ID
- [ ] Returns 429 when exceeded

### Backend implementation
- [ ] Redis-backed sliding window (ZADD + ZREMRANGEBYSCORE) as primary
- [ ] In-memory token bucket fallback when Redis unavailable
- [ ] Startup warning logged when running in-memory mode
- [ ] Rate limiting disabled entirely in SQLite mode
- [ ] Rate limit rules configurable at runtime via `PUT /api/v1/admin/rate-limits`
- [ ] Only admin users can read/update rate limit rules
- [ ] Bypass token (`MODULO_RATELIMIT_BYPASS_TOKEN`) skips rate limiting
- [ ] Client keyed by `X-Forwarded-For` IP + request path
- [ ] Only POST/PUT/PATCH methods are rate limited
- [ ] `Retry-After` header set to the window duration in seconds

### Concurrency & multi-worker
- [ ] Redis coordinates rate limit state across multiple worker processes
- [ ] In-memory fallback is per-process — effectively doubles limit on N replicas
- [ ] `Retry-After` response returned before the request handler runs (middleware order)

### Unit test coverage
- [ ] TokenBucket: consume when tokens available
- [ ] TokenBucket: blocks when empty, refills over time
- [ ] TokenBucket: burst ceiling enforced
- [ ] RedisSlidingWindowRateLimiter: allows within limit
- [ ] RedisSlidingWindowRateLimiter: blocks over limit
- [ ] RedisSlidingWindowRateLimiter: exact boundary at limit
- [ ] RedisSlidingWindowRateLimiter: custom key prefix
- [ ] RedisSlidingWindowRateLimiter: custom window duration
- [ ] RateLimiterRegistry: in-memory fallback by default
- [ ] RateLimiterRegistry: uses Redis when available
- [ ] RateLimiterRegistry: Redis blocks over limit
- [ ] Middleware: accepts explicit settings injection
- [ ] Middleware: accepts explicit registry injection
- [ ] Middleware: valid bypass token skips rate limiting
- [ ] Middleware: invalid bypass token does not skip
- [ ] Middleware: returns 429 with `rate_limit_exceeded` error code
- [ ] Middleware: GET requests not rate limited
- [ ] Middleware: MCP paths rate limited
- [ ] Admin API: GET returns rules and mode
- [ ] Admin API: PUT updates rules dynamically
- [ ] Admin API: non-admin gets 403

### Edge cases
- [ ] Missing `X-Forwarded-For` header falls back to `request.client.host`
- [ ] Unknown client host falls back to `"unknown"`
- [ ] Empty bypass token header treated as no token
- [ ] Empty `modulo_ratelimit_bypass_token` setting disables bypass entirely
- [ ] Redis connection failure falls back gracefully to in-memory
- [ ] Negative or zero `max_requests` rejected at schema level (Field(gt=0))
- [ ] `window_s` less than 1 rejected at schema level (Field(ge=1))
- [ ] Runtime rules update does not reset existing in-flight counters
- [ ] At least one rule required on PUT (400 if empty)
- [ ] Path prefix matching: `/api/v1/runs` matches both exact and sub-routes
- [ ] TokenBucket thread-safe via `asyncio.Lock`

## Known Gaps
- BDD feature file at `backend/tests/bdd/features/model_backends/rate_limiting.feature` is a placeholder — no real scenarios
- No BDD coverage for any rate limit endpoint or behaviour
- No integration/E2E test that exercises Redis sliding window against a real Redis
- MCP-specific rate limit rules (`trigger_pipeline` vs general MCP calls) are not differentiated in middleware — all `/mcp` paths share 200 req/min rule
- Middleware default rules (100/60 for runs, 50/60 for triggers, 30/60 for HITL) differ from PRD spec (60/60 for runs, 100/60 for webhooks, 20/60 for HITL) — drift not documented as intentional
