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

  @awaiting-implementation
  Scenario: Authorization request with PKCE
    Given an OAuth client exists with id "oauth_client_1"
    When I POST /mcp/oauth/authorize with response_type "code" and client_id "oauth_client_1" and redirect_uri "https://app.example.com/callback" and scope "trigger:run" and code_challenge "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM" and code_challenge_method "S256" and state "xyz"
    Then the response status is 200
    And the response contains a code parameter
    And the response contains the state "xyz"

  @awaiting-implementation
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

  Scenario: Token is scoped to the registered scope set
    Given an OAuth client exists with id "limited_client" and scopes ["trigger:run"]
    When the client requests a token with scope "hitl:review"
    Then the response status is 400
    And the error indicates "invalid_scope"

  Scenario: Invalid redirect_uri is rejected during authorization
    Given an OAuth client exists with id "oauth_client_1" and redirect_uris ["https://app.example.com/callback"]
    When I POST /mcp/oauth/authorize with client_id "oauth_client_1" and redirect_uri "https://evil.com/phish"
    Then the response status is 400
    And the error indicates "redirect_uri not allowed"

  @awaiting-implementation
  Scenario: Refresh token rotation issues a new pair
    Given a token family "family_1" at sequence 0 for client "oauth_client_1"
    When I POST /mcp/oauth/token with grant_type "refresh_token" and refresh_token "rt1" and client_id "oauth_client_1" and client_secret "correct_secret"
    Then the response status is 200
    And the response contains a new access_token
    And the response contains a new refresh_token
    And the old refresh token is no longer valid

  @awaiting-implementation
  Scenario: PKCE code verifier is required on token exchange
    Given an authorization code "auth_code_pkce" was created with code_challenge "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    When I POST /mcp/oauth/token with authorization code "auth_code_pkce" and no code_verifier
    Then the response status is 400
    And the error indicates "code_verifier required"

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
