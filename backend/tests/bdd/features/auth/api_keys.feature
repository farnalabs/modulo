Feature: API Key Management
  As an admin user
  I want to create, list, revoke, and update API keys
  So that MCP clients can authenticate programmatically

  Scenario: Admin creates an API key
    Given I am authenticated as an admin in org "acme"
    When I POST /api/v1/api-keys with name "ci-token" and role "operator"
    Then the response status is 201
    And the response contains a full_key starting with "mk_"
    And the response has name "ci-token"

  Scenario: Non-admin cannot create an API key
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/v1/api-keys with name "ci-token" and role "operator"
    Then the response status is 403

  Scenario: Admin revokes an API key
    Given I am authenticated as an admin in org "acme"
    And an API key "my-key" exists
    When I DELETE /api/v1/api-keys/{key_id}
    Then the response status is 200
    And the response indicates the key is revoked

  Scenario: Listing API keys returns only active keys
    Given I am authenticated as an admin in org "acme"
    And an API key "my-key" exists
    When I GET /api/v1/api-keys
    Then the response status is 200
    And the response contains key "my-key"

  Scenario: Invalid API key is rejected
    Given I have a valid API key
    When I make an authenticated request with the wrong API key
    Then the response status is 401
