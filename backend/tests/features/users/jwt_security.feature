Feature: JWT Security
  As a user
  I want my authentication tokens to be secure
  So that my session cannot be hijacked or forged

  Scenario: Login returns access and refresh tokens
    Given a user exists with email "alice@example.com" and password "correct-horse-battery"
    When I POST /api/auth/login with email "alice@example.com" and password "correct-horse-battery"
    Then the response status is 200
    And the response contains an access_token
    And the response contains a refresh_token

  Scenario: Access token grants access to protected endpoints
    Given I have a valid JWT for org "acme"
    When I make an authenticated request to /api/auth/me
    Then the response status is 200
    And I see my user profile

  Scenario: Expired access token is rejected
    Given I have an expired JWT for org "acme"
    When I make an authenticated request to /api/auth/me
    Then the response status is 401

  Scenario: Refresh token rotates the token pair
    Given I am logged in as "alice@example.com"
    When I POST /api/auth/refresh with my refresh token
    Then the response status is 200
    And the response contains a new access_token
    And the response contains a new refresh_token
    And the new tokens differ from the old pair

  Scenario: Reusing a refresh token is detected as theft
    Given I have a refresh token with sequence 0
    When I refresh my tokens once
    And I refresh my tokens again with the same refresh token
    Then the response status is 401
    And the error indicates suspected theft

  Scenario: Logout blacklists the token family
    Given I am logged in as "alice@example.com"
    When I POST /api/auth/logout with my refresh token
    Then the response status is 200
    And subsequent refresh attempts are rejected

  Scenario: Token with invalid signature is rejected
    Given I have a tampered JWT for org "acme"
    When I make an authenticated request to /api/auth/me
    Then the response status is 401

  Scenario: Token with alg=none is rejected
    Given I have a JWT with alg=none for org "acme"
    When I make an authenticated request to /api/auth/me
    Then the response status is 401
