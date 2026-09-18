Feature: MCP OAuth 2.0 Authorization
  As an MCP client developer
  I want to register OAuth clients and perform the authorization code flow
  So that MCP tools can be accessed with scoped access tokens

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin registers an OAuth client
    When I POST /api/v1/mcp/oauth/clients with name "My MCP App" and redirect_uris ["https://app.example.com/callback"] and scopes ["trigger:run", "hitl:review"]
    Then the response status is 201
    And the response contains client_id
    And the response contains client_secret
    And the response has name "My MCP App"

  Scenario: Authorization request with PKCE redirects the browser to the consent route
    Given an OAuth client exists with id "oauth_client_1"
    When I GET /mcp/oauth/authorize with client_id "oauth_client_1" and redirect_uri "https://app.example.com/callback" and scope "trigger:run" and code_challenge "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" and code_challenge_method "S256" and state "xyz"
    Then the response status is 302
    And the response redirects to the SPA consent route
    And the consent-state row was created with the code_challenge

  Scenario: Approving the consent mints an account-bound code
    Given a pending consent state "state-xyz" for client "oauth_client_1"
    When I POST /api/v1/mcp/oauth/consent/approve with state "state-xyz"
    Then the response status is 200
    And the response returns a server-derived redirect URL

  Scenario: Approving without an authenticated session is denied
    Given I am not authenticated
    When I POST /api/v1/mcp/oauth/consent/approve with state "state-xyz"
    Then the response status is 401

  Scenario: Approving an unknown or consumed consent state is denied
    Given an already-consumed consent state "state-consumed"
    When I POST /api/v1/mcp/oauth/consent/approve with state "state-consumed"
    Then the response status is 400

  Scenario: Token exchange exchanges authorization code for tokens
    Given an authorization code "auth_code_abc" exists for client "oauth_client_1"
    When I POST /mcp/oauth/token with grant_type "authorization_code" and code "auth_code_abc" and client_id "oauth_client_1" and client_secret "correct_secret" and redirect_uri "https://app.example.com/callback" and code_verifier "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    Then the response status is 200
    And the response contains an access_token
    And the token has scopes ["trigger:run"]

  Scenario: Token exchange with unknown client_id returns error
    Given an authorization code "auth_code_abc" exists for client "oauth_client_1"
    When I POST /mcp/oauth/token with grant_type "authorization_code" and code "auth_code_abc" and client_id "unknown_client" and client_secret "wrong_secret" and redirect_uri "https://app.example.com/callback" and code_verifier "verifier"
    Then the response status is 400
    And the error indicates "invalid_grant"

  Scenario: Token exchange with used authorization code returns error
    Given a used authorization code "used_code" exists for client "oauth_client_1"
    When I POST /mcp/oauth/token with grant_type "authorization_code" and code "used_code" and client_id "oauth_client_1" and client_secret "correct_secret" and redirect_uri "https://app.example.com/callback" and code_verifier "verifier"
    Then the response status is 400
    And the error indicates "invalid_grant"

  Scenario: Authorize rejects a scope outside the client's allowed set
    Given an OAuth client exists with id "limited_client" and scopes ["trigger:run"]
    When I GET /mcp/oauth/authorize with client_id "limited_client" and redirect_uri "https://app.example.com/callback" and scope "hitl:review" and code_challenge "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" and code_challenge_method "S256" and state "xyz"
    Then the response status is 400
    And the error indicates "invalid_scope"

  Scenario: Invalid redirect_uri is rejected during authorization
    Given an OAuth client exists with id "oauth_client_1" and redirect_uris ["https://app.example.com/callback"]
    When I GET /mcp/oauth/authorize with client_id "oauth_client_1" and redirect_uri "https://evil.com/phish" and scope "trigger:run" and code_challenge "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" and code_challenge_method "S256" and state "xyz"
    Then the response status is 400
    And the error indicates "redirect_uri not allowed"

  Scenario: Refresh token issues a new pair
    Given a refresh token "rt1" for client "oauth_client_1" with scopes ["trigger:run"]
    When I POST /mcp/oauth/refresh with grant_type "refresh_token" and refresh_token "rt1" and client_id "oauth_client_1" and client_secret "correct_secret"
    Then the response status is 200
    And the response contains a new access_token
    And the response contains a new refresh_token

  Scenario: A demoted account cannot refresh a token whose scopes exceed its live role
    Given a refresh token "rt_operator" for client "oauth_client_1" with scopes ["hitl:review"] issued to an account demoted to viewer
    When I POST /mcp/oauth/refresh with grant_type "refresh_token" and refresh_token "rt_operator" and client_id "oauth_client_1" and client_secret "correct_secret"
    Then the response status is 400
    And the error indicates "invalid_grant"

  Scenario: PKCE code verifier is required on token exchange
    Given an authorization code "auth_code_pkce" was created with code_challenge "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    When I POST /mcp/oauth/token with authorization code "auth_code_pkce" and no code_verifier
    Then the response status is 400
    And the error indicates "invalid_grant"

  Scenario: Admin revokes an OAuth client
    Given an OAuth client exists with id "oauth_client_1"
    When I DELETE /api/v1/mcp/oauth/clients/oauth_client_1
    Then the response status is 200
    And the response indicates the client is deleted
    And the client cannot be used for token exchange

  Scenario: Non-admin cannot register an OAuth client
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/v1/mcp/oauth/clients with name "My App" and redirect_uris ["https://app.example.com/callback"] and scopes ["trigger:run"]
    Then the response status is 403
