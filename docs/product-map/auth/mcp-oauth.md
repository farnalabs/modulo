---
id: feat-auth-mcp-oauth
prd: 6.4
delivery-tasks: [task-nv9-mcp-oauth]
bdd:
  - backend/tests/features/mcp/mcp_oauth.feature
code:
  - backend/src/modulo/api/routes/mcp_oauth.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/auth/oauth.py
  - backend/src/modulo/db/models/oauth_client.py
  - backend/src/modulo/db/models/oauth_token.py
depends-on: [feat-core-oidc-integration]
status: partial
---
# MCP OAuth 2.0 Authorization Code Flow

OAuth 2.0 authorization code grant for MCP client authentication, with client registration, token family rotation, theft detection, and dual-layer scope enforcement.

## Behaviours

### OAuth Client Management (CRUD)
- [x] Admin/operator users can register OAuth 2.0 clients with name, redirect URIs, and scopes via POST /api/v1/mcp/oauth/clients
- [x] Client registration returns 201 with client_id, client_secret (shown once), and id
- [x] Non-admin/operator users receive 403 on register
- [x] List all OAuth clients for the org via GET /api/v1/mcp/oauth/clients
- [x] Empty org returns empty list
- [x] Registered clients appear in list
- [x] Delete an OAuth client via DELETE /api/v1/mcp/oauth/clients/{client_id}
- [x] Deletion cascades to associated authorization codes and token families
- [x] Non-admin/operator users receive 403 on delete
- [x] Deleting a non-existent client returns 404
- [x] Missing or empty name, redirect_uris, or scopes returns 422
- [x] Invalid scope names return 400 with detail from InvalidScopeError
- [x] Missing/unconfigured MODULO_PUBLIC_URL returns 500 on client creation
- [x] Unauthenticated access to client management returns 401/403

### Authorization Code Grant
- [x] POST /mcp/oauth/authorize with response_type=code issues a one-time authorization code
- [x] Unsupported response_type returns 400 with unsupported_response_type
- [x] Missing client_id or redirect_uri returns 400
- [x] Unknown client_id returns 400 with invalid_client
- [x] redirect_uri not in client's allowed URIs returns 400
- [x] Requested scopes outside client's allowed scopes return 400
- [x] Unknown scope values return 400
- [x] Authorization codes expire after 10 minutes
- [x] Expired codes return invalid_grant on token exchange
- [x] Authorization codes are single-use — second consume returns invalid_grant
- [x] Code issued to one client cannot be consumed by another client
- [x] redirect_uri mismatch between authorize and token exchange returns invalid_grant
- [x] Client secret is compared via HMAC (not plaintext)
- [x] Client secret is stored as SHA-256 hash only

### Token Exchange
- [x] POST /mcp/oauth/token with grant_type=authorization_code returns access token
- [x] Unsupported grant_type returns 400 with unsupported_grant_type
- [x] Missing code, redirect_uri, client_id, or client_secret returns 400
- [x] Invalid client credentials return invalid_client
- [x] Access token is a JWT with purpose=oauth_access, sub, org_id, scopes, token_family, token_sequence, iat, exp
- [x] Access token expires in 60 minutes (expires_in=3600 in response)
- [x] Token exchange creates a new token family (family_id, sequence=0)
- [x] Response includes access_token, token_type=Bearer, expires_in, and scope

### Token Family Rotation (Theft Detection)
- [x] Each token exchange creates a new OAuthTokenFamily
- [x] Token families track max_sequence to detect out-of-order usage
- [x] Out-of-order sequence (theft) blacklists the family and returns invalid_grant
- [x] Blacklisted families persist and subsequent token checks return invalid_grant
- [x] Token family can be explicitly revoked via blacklist
- [x] check_oauth_token_family_valid queries non-blacklisted families

### MCP Auth — OAuth Token Support
- [ ] McpAuthMiddleware accepts Bearer tokens with JWT purpose=oauth_access — no integration test exists
- [ ] Invalid/expired/malformed OAuth tokens return 401 — no integration test exists
- [ ] Token without oauth_access purpose is rejected — no integration test exists
- [ ] Token family blacklist checked on every authenticated MCP request — no integration test exists
- [ ] Role derived from OAuth scopes: hitl:review → operator, trigger:run or library:browse → runner — no integration test exists
- [x] OAuth protocol endpoints (/mcp/oauth/authorize, /mcp/oauth/token) bypass Bearer auth
- [x] Health check endpoints bypass all auth

### Dual-Layer Scope Enforcement
- [ ] Token scopes validated at middleware (McpAuthMiddleware) — middleware resolves role only, does not validate scopes; defer to ViewModel layer
- [x] Token scopes validated at ViewModel tool layer (per-tool check) — implemented via check_tool_scope in scope_validator.py
- [x] ViewModel rejects commands exceeding token scope even if middleware passed
- [x] SSE event streams validate org context on every event — validate_current_auth() checks token family blacklist per-call

### Unit Test Coverage
- [x] TestRegisterOAuthClient.test_create_returns_201_with_secret
- [x] TestRegisterOAuthClient.test_create_rejects_missing_name
- [x] TestRegisterOAuthClient.test_create_rejects_empty_redirect_uris
- [x] TestRegisterOAuthClient.test_create_rejects_empty_scopes
- [x] TestRegisterOAuthClient.test_create_runner_gets_403
- [x] TestRegisterOAuthClient.test_create_disallows_invalid_scopes
- [x] TestRegisterOAuthClient.test_create_requires_public_url
- [x] TestListOAuthClients.test_list_returns_200
- [x] TestListOAuthClients.test_list_empty
- [x] TestDeleteOAuthClient.test_delete_returns_200
- [x] TestDeleteOAuthClient.test_delete_not_found_returns_404
- [x] TestDeleteOAuthClient.test_delete_runner_gets_403
- [x] test_list_returns_401_without_auth
- [x] TestAuthorizeErrors.test_unsupported_response_type
- [x] TestAuthorizeErrors.test_missing_client_id
- [x] TestAuthorizeErrors.test_missing_redirect_uri
- [x] TestAuthorizeErrors.test_unknown_client_id
- [x] TestTokenExchangeErrors.test_unsupported_grant_type
- [x] TestTokenExchangeErrors.test_missing_params
- [x] TestConsumeAuthorizationCode.test_expired_code
- [x] TestConsumeAuthorizationCode.test_used_code
- [x] TestConsumeAuthorizationCode.test_wrong_client
- [x] TestConsumeAuthorizationCode.test_redirect_uri_mismatch

### BDD Coverage
- [x] BDD feature file at backend/tests/features/mcp/mcp_oauth.feature exists with scenarios
- [x] Authorize flow: valid client receives code
- [x] Authorize flow: unknown client_id returns error
- [x] Token exchange: valid code returns access token
- [x] Token exchange: used code returns error
- [x] Token exchange: expired code returns error — unit test exists but no BDD scenario (needs precise datetime mocking for expiry)

## Error Handling
- [ ] POST /api/v1/mcp/oauth/clients returns 501 if DB migration not run (ProgrammingError)
- [ ] GET /api/v1/mcp/oauth/clients returns 501 if DB migration not run (ProgrammingError)
- [ ] DELETE /api/v1/mcp/oauth/clients/{id} returns 501 if DB migration not run (ProgrammingError)
- [ ] POST /api/v1/mcp/oauth/clients returns 503 on SQLAlchemyError (DB connection failure)
- [ ] GET /api/v1/mcp/oauth/clients returns 503 on SQLAlchemyError
- [ ] DELETE /api/v1/mcp/oauth/clients/{id} returns 503 on SQLAlchemyError
- [ ] _oauth_authorize catches JSON decode error → 400 invalid_request
- [ ] _oauth_token catches JSON decode error → 400 invalid_request
- [ ] _oauth_authorize broad except Exception → 400 with error detail
- [ ] _oauth_token broad except Exception → 400 invalid_grant
- [ ] McpAuthMiddleware OAuth path catches JWTError → 401
- [ ] McpAuthMiddleware OAuth path catches Exception on family check → 401

## Additional Edge Cases
- [x] client_secret never stored in plaintext — only SHA-256 hash persisted
- [x] OAuthAuthorizationCode.used flag prevents replay attacks
- [x] RLS scoped to organisation for all OAuth data access
- [x] Unauthenticated requests without any Bearer token return 401
- [x] MODULO_PUBLIC_URL validation gate prevents misconfigured OAuth flows
- [x] Invalid/expired authorization codes handled in consume_authorization_code
- [x] Code-to-client binding prevents cross-client consumption
- [x] redirect_uri consistency enforced between authorize and token exchange
- [ ] Client deletion cascades to auth codes and token families — verified in test
- [ ] MODULO_PUBLIC_URL checked in both authorize and CRUD routes
- [ ] State parameter echoed but not validated server-side — potential CSRF gap
- [ ] Code challenge_method accepted but not stored or validated (PKCE unimplemented)

## Resilience & Integration Robustness
- [ ] Database session management: all CRUD routes use `async with session.begin()` for atomicity
- [ ] Broad except Exception in protocol endpoints prevents crash but may mask errors
- [ ] No timeout on authorization code expiry enforcement (10-min TTL inherent, not externally configurable)
- [ ] Rate limiting via RateLimiterMiddleware applied to token exchange endpoint
- [ ] OAuth token family blacklist checked on every middleware validation
- [ ] Session factory failure in _oauth_authorize/_oauth_token would propagate as 500
- [ ] McpAuthMiddleware OAuth path has no unit test coverage

## QA History
- 2026-07-05: Cross-cutting QA (index 159): Fixed status covered→partial. Added ProgrammingError→501 and SQLAlchemyError→503 catches to all 3 CRUD routes with _log.warning calls. Added Error Handling section (12 checkboxes), Additional Edge Cases section (12 checkboxes), Resilience & Integration Robustness section (7 checkboxes). Added QA History section. Documented PKCE gap: BDD scenarios pass via mocked step definitions, not real implementation. Documented refresh_token gap: grant_type unimplemented, BDD scenarios marked xfail. Created website docs stub at Website/modulo-website/src/docs/mcp-oauth.md. Status: partial.

## Remaining Gaps

- **Scope naming mismatch with PRD**: PRD §6.4 specifies `pipelines:read`, `pipelines:run`, `hitl:approve`, `library:read`, `library:write`. Code implements `trigger:run`, `hitl:review`, `library:browse` in VALID_SCOPES.
- **PKCE unimplemented but BDD claims it works**: BDD scenarios "Authorization request with PKCE" and "PKCE code verifier required" pass only because step definitions (test_mcp_oauth.py) mock the PKCE behavior. The _oauth_authorize handler receives code_challenge but never stores it on OAuthAuthorizationCode. The _oauth_token handler never validates code_verifier against a stored code_challenge.
- **BDD refresh_token scenarios are xfail**: 2 BDD scenarios and 2 unit tests for refresh token rotation are marked @pytest.mark.xfail because the _oauth_token handler only supports authorization_code grant_type. The refresh_token grant type and refresh token issuance are not implemented.
- **No middleware OAuth path tests**: McpAuthMiddleware's OAuth token validation path (JWT decode → family blacklist check → scope-to-role mapping) has zero unit or integration tests.
- **state parameter not validated**: The authorize endpoint accepts state and echoes it back but performs no server-side validation or CSRF binding.
- **No `authlib` usage**: PRD mandates `authlib` (not hand-rolled). Current implementation uses `python-jose` directly for JWT encoding/decoding. OAuth logic is hand-rolled in `modulo/auth/oauth.py`.
- **No per-pipeline scopes**: `hitl:approve:pipeline:{id}` scope pattern from PRD is not implemented at any layer.
- **`library:write` scope not implemented**: Only `library:browse` exists in code; no write scope for library primitives exists.
- **No BDD feature files exist for MCP tools**: The `features/mcp/` directory exists but only contains `mcp_oauth.feature`. Step definitions in `test_alpha_mcp.py` reference `../../features/mcp/trigger.feature`, `review_hitl.feature`, `human_only.feature`, `library_browse.feature`, `onboarding.feature` — none of these files exist. This means 5 BDD feature files are missing alongside the OAuth-specific one.
- **SSE per-event org validation**: Code comment asserts org context is validated per-event for streaming connections, but the current implementation validates only at tool/resource call time via `validate_current_auth()`. No server-push SSE event validation path exists.
- **`MODULO_PUBLIC_URL` hardening**: Localhost check (`settings.modulo_public_url == "http://localhost:8000"`) is fragile — any local dev server on a different port or 127.0.0.1 will bypass the guard.
- **BDD authorize steps previously used GET instead of POST**: The handler expects POST+JSON, but BDD steps used GET+params. Fixed in this QA pass.
- **Token exchange response does not include refresh_token**: PRD mentions refresh tokens but they are not emitted in the current token exchange response.