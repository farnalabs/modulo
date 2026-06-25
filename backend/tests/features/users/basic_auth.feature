Feature: Basic Auth
  As a user
  I want to authenticate with email and password
  So that I can access my organisation's Modulo instance

  Scenario: Successful login returns JWT
    Given a user exists with email "alice@example.com" and password "correct-horse-battery"
    When I POST /api/auth/login with email "alice@example.com" and password "correct-horse-battery"
    Then the response status is 200
    And the response contains an access_token
    And the token encodes org_id

  Scenario: Wrong password is rejected
    Given a user exists with email "alice@example.com" and password "correct-horse-battery"
    When I POST /api/auth/login with email "alice@example.com" and password "wrong-password"
    Then the response status is 401

  Scenario: Unknown email is rejected
    When I POST /api/auth/login with email "nobody@example.com" and password "anything"
    Then the response status is 401

  Scenario: Expired token is rejected
    Given I have an expired JWT for org "acme"
    When I make an authenticated request to /api/pipelines
    Then the response status is 401

  Scenario: Token refresh works
    Given a user exists with email "alice@example.com" and password "correct-horse-battery"
    When I POST /api/auth/login with email "alice@example.com" and password "correct-horse-battery"
    And I use the refresh_token to get a new access_token
    Then the new access_token is valid
