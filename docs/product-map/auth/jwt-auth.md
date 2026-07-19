---
id: feat-auth-jwt-auth
prd: 7.10
delivery-tasks: []
code:
  - backend/src/modulo/auth/jwt.py
  - backend/src/modulo/api/routes/auth.py
bdd:
  - backend/tests/bdd/features/auth/jwt_security.feature
  - backend/tests/bdd/features/teams/stale_jwt_revocation.feature
unit-tests:
  - backend/tests/unit/auth/test_jwt.py
  - backend/tests/unit/api/test_jwt_security_bdd.py
  - backend/tests/unit/api/test_ws_token.py
  - backend/tests/unit/auth/test_settings.py
  - backend/tests/unit/api/test_auth_programming_error.py
depends-on: []
status: partial
---

# JWT Auth

JWT-based authentication with access tokens, refresh tokens, token family rotation,
WebSocket auth tokens, algorithm pinning, and SECRET_KEY entropy enforcement (PRD §7.10).

## Behaviours

### Access Tokens
- [x] Access token returned on successful login (PRD §7.10)
- [x] Access token has 15-minute expiry (PRD §7.10)
- [x] Access token grants access to protected endpoints (`/api/auth/me`)
- [x] Expired access token is rejected with 401
- [x] Token with invalid signature is rejected with 401
- [x] Token with `alg=none` is rejected (algorithm pinning to HS256)
- [x] Token carries org context (`org_id`, `org_role`, `account_id`)
- [x] Token with missing `sub` claim raises JWTError
- [x] Token with empty `sub` claim raises JWTError
- [x] Token without `account_id` raises JWTError
- [x] Malformed org_id in token resolves to `None` (graceful degradation)
- [x] Access token is set as `modulo_session` HTTP-only cookie on login

### Refresh Tokens
- [x] Refresh token returned on successful login alongside access token
- [x] Refresh token has 7-day (168-hour) expiry (PRD §7.10)
- [x] Refresh token contains `token_family` and `token_sequence` claims
- [x] Refresh token has `purpose: refresh` claim
- [x] Refresh endpoint accepts refresh token and returns new token pair
- [x] Refresh token rotation: new tokens differ from old pair
- [x] Reusing a refresh token (stalest sequence) detects theft — returns 401
- [x] Theft-detected error message includes "theft" or "revoked"
- [x] Refresh token with wrong key raises JWTError
- [x] Access token rejected by refresh endpoint (purpose check)
- [x] WS token rejected by refresh endpoint (purpose check)
- [x] Refresh endpoint carries org context through to new tokens

### Token Family Management
- [x] Token family created on login (`create_family`)
- [x] Logout blacklists the token family (`blacklist_family`)
- [x] Subsequent refresh attempts after logout are rejected (401)
- [x] Sequence is advanced on each refresh (`advance_sequence`)
- [x] Stale sequence presentation triggers full family invalidation
- [x] Admin can revoke sessions for a user (stale JWT revocation)

### WebSocket Auth Tokens
- [x] WS token endpoint returns 200 with `ws_token` and `expires_in_seconds`
- [x] WS token JWT has `purpose: ws` claim
- [x] WS token JWT carries user identity
- [x] WS token JWT expiry matches configured TTL (default 60s)
- [x] Regular access token rejected by WS purpose check
- [x] WS token rejected with wrong key
- [x] WS token endpoint requires authentication (returns 401/403 without token)
- [x] Redis-based opaque WS token created when Redis is available
- [x] JWT-based WS token fallback when Redis is unavailable
- [x] WS token type in response reflects token type (`ws-jwt` or `ws-opaque`)

### Algorithm Pinning & SECRET_KEY
- [x] JWT decode uses `algorithms=["HS256"]` explicitly
- [x] `none` algorithm rejected — algorithm confusion attack prevented at library level
- [x] SECRET_KEY minimum 32 bytes enforced at startup via Settings validation
- [x] Blocked placeholder SECRET_KEY values rejected at startup (`changeme`, `secret`, etc.)
- [x] SECRET_KEY exactly 32 bytes passes validation
- [x] SECRET_KEY longer than 32 bytes passes validation

### Auth Rate Limiting
- See [rate-limiting.md](rate-limiting.md#auth-rate-limiting-610) for login rate limiting behaviour
- [ ] Auth rate limiting covered by BDD feature scenario (covered by unit tests only: `test_auth_rate_limiter.py`)

### Claim Tokens (HITL gates)
- [x] Claim token created with `run_id`, `gate_id`, `client_id`
- [x] Claim token has 15-minute default expiry
- [x] Claim token supports custom expiry via `expiry_minutes`
- [x] Claim token validated against expected `run_id` and `gate_id`
- [x] Claim token with wrong key raises JWTError
- [x] Claim token with wrong `run_id` raises JWTError
- [x] Claim token with wrong `gate_id` raises JWTError
- [x] Expired claim token raises JWTError
- [x] Claim token without purpose raises JWTError
- [x] Claim token with wrong purpose raises JWTError

### Error Handling
- [x] login() DB failure returns 501 Not Implemented with migration hint
- [x] login() SQLAlchemyError returns 503 Service Unavailable
- [x] login() IntegrityError on token family creation returns 409 Conflict
- [x] refresh() DB failure returns 501 Not Implemented with migration hint
- [x] refresh() SQLAlchemyError returns 503 Service Unavailable
- [x] logout() DB failure returns 501 Not Implemented with migration hint
- [x] logout() SQLAlchemyError returns 503 Service Unavailable
- [x] me() DB failure returns 501 Not Implemented with migration hint
- [x] me() SQLAlchemyError returns 503 Service Unavailable

## Edge Cases
- [x] System admin without memberships can still log in (`requires_bootstrap=true`)
- [x] User with no org memberships returns 403 on login
- [x] Rate limiting in-memory mode handles concurrent IPs independently
- [x] Redis failure falls back gracefully to in-memory rate limiting
- [x] Redis failure for WS token falls back to JWT-based token
- [x] Cookie value cleared on logout (`modulo_session` and `XSRF-TOKEN` set to empty, max_age=0)
- [x] `FERNET_KEY` and `SECRET_KEY` are separate keys with separate purposes
- [x] CSRF token (`XSRF-TOKEN`) rotated on every login (not just set once)
- [x] logout() with already-blacklisted family is idempotent (200, warning logged, family unaffected)

## Known Gaps
- Auth rate limiting has no BDD feature scenario (covered by unit tests only)
- WS token scenario is tested via unit tests but not via BDD feature file
- No integration test for token family invalidation with real Redis
- No integration test for WS token opaque path with real Redis
- BDD test mock infrastructure is incomplete — login/logout scenarios with DB mocking fail due to `list_memberships_for_account` coroutine iteration issue
- No end-to-end test for stale JWT revocation flow through admin UI
- WS token single-use enforcement (opaque token path) is tested in unit tests but has no BDD coverage
- SECRET_KEY at-rest encryption validation (that checkpoint blobs remain encrypted after key rotation)
- No integration test verifying ProgrammingError→501 with real Postgres (unit-tested via mocks only)

## QA History

### 2026-07-12 — Round 3 re-QA (improve-architecture auth remaining)
- Fixed operator precedence bug in auth.py:100 (`not a or not b and c` → `not a or (limiter is not None and not b)`)
- Removed unused `# noqa: S106` directives on ws_token_type lines, then re-added with proper intent coverage
- Verified all claimed Round 2 fixes (B904, CancelledError, dead code removal) are correctly applied
- Status: partial

### 2026-07-01 — Cross-cutting QA (improve-architecture index 25)
- Fixed access token expiry: 60m → 15m (PRD §7.10 compliance)
- Fixed refresh token expiry: 24h → 168h (7 days, PRD §7.10 compliance)
- Fixed JWT WS token fallback to use configured TTL instead of hardcoded 15m
- Fixed BDD step definitions referencing renamed functions
- Fixed stale JWT revocation step definition pattern (singular vs plural)
- Added `test_ws_token_jwt_expiry_matches_settings` unit test
- Enriched product map entry with full behaviour list (was a stub)
- Status changed from `gap` to `partial`

### 2026-07-05 — Cross-cutting QA (improve-architecture index 149)
- Fixed CRITICAL: added ProgrammingError→501 catches to all 4 DB-accessing auth routes (login, refresh, logout, me)
- Fixed MAJOR: logout() now checks blacklist_family result and logs warning if family not found
- Created test_auth_programming_error.py (5 test cases: 4 ProgrammingError→501 + 1 idempotent logout)
- Added Error Handling section (4 behaviour checkboxes) to product map
- Added 2 new edge cases (blacklisted family idempotent, login DB failure 501)
- Added unit-tests frontmatter ref to new test file
- Status: partial (9 known gaps remain)

### 2026-07-08 — Cross-cutting QA (improve-architecture index 256)
- Fixed CRITICAL: added SQLAlchemyError→503 catches to all 4 auth routes (login, refresh, logout, me) — connection/deadlock failures previously propagated as raw 500
- Fixed CRITICAL: added IntegrityError→409 catch on login route (token family creation race)
- Fixed MAJOR: corrected BDD feature file API paths from `/api/auth/` to `/api/v1/auth/` to match actual router prefix
- Fixed MAJOR: `test_none_algorithm_rejected` now properly verifies decode-time rejection of `alg: none` tokens (manually crafted JWT bypasses PyJWT encode-time validation)
- Added 5 new tests in test_auth_programming_error.py (integrity error→409 on login, SQLAlchemyError→503 on login, refresh, logout, me)
- Updated Error Handling section with 5 new [x] checkboxes
- 9 existing tests + 5 new tests all pass

### 2026-07-11 — Round 2 re-QA (improve-architecture index 387)
- Fixed B904: added `from None` to all `except IntegrityError`, `except ProgrammingError`, `except SQLAlchemyError` handlers (prevented internal exception chain leakage in responses)
- Fixed B904: added `asyncio.CancelledError: raise` guards before all `except Exception` blocks (login, refresh, logout inner, ws_token, me, csrf_token) — prevented silent CancelledError suppression
- Removed dead `except IntegrityError` handler from `me()` endpoint (SELECT-only query cannot raise IntegrityError)
- Updated product map: added Round 2 QA entry
