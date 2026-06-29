Feature: Password Change
  As a logged-in user
  I want to change my password
  So that I can maintain account security

  Scenario: Successful password change
    Given I am authenticated in org "acme"
    When I change my password from "correct-horse-battery" to "new-strong-password-42"
    Then the response status is 200
    And the response says "Password changed successfully"

  Scenario: Wrong current password is rejected
    Given I am authenticated in org "acme"
    When I change my password from "wrong-password-42" to "new-strong-password-42"
    Then the response status is 400
    And the error mentions "incorrect"

  Scenario: Low-entropy new password is rejected
    Given I am authenticated in org "acme"
    When I change my password from "correct-horse-battery" to "11111111"
    Then the response status is 422
    And the error mentions "entropy"

  Scenario: SSO user without local password cannot change password
    Given I am authenticated in org "acme"
    When I attempt to change my password without a local password set
    Then the response status is 400

  Scenario: Password change invalidates existing sessions
    Given I am authenticated in org "acme"
    When I change my password from "correct-horse-battery" to "new-strong-password-42"
    Then all token families for my user are blacklisted
