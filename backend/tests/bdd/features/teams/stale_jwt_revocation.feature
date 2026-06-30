Feature: Stale JWT Revocation
  As an admin
  I want to immediately revoke a user's team membership tokens
  So that removed members lose access without waiting for JWT expiry

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin can revoke all tokens for a user
    Given a user "alice" exists
    When I revoke sessions for user "alice"
    Then all tokens for "alice" are invalidated
