Feature: Remy Access Control
  As an org admin
  I want to control who can access the Remy AI assistant
  So that access is restricted to authorised users

  Scenario: Remy is accessible when user is on the access list
    Given I am authenticated as an admin in org "acme"
    And the Remy access list includes my user_id
    When I check remy access
    Then the response status is 200
    And access is granted

  Scenario: Admin always has access regardless of access list
    Given I am authenticated as an admin in org "acme"
    When I check remy access
    Then the response status is 200
    And access is granted

  Scenario: User with matching org_role has access
    Given I am authenticated as a viewer in org "acme"
    And the Remy access list includes role "viewer"
    When I check remy access
    Then the response status is 200
    And access is granted

  Scenario: User with matching team_id has access
    Given I am authenticated as an admin in org "acme"
    And the Remy access list includes team_id "team-engineering"
    And I belong to team "team-engineering"
    When I check remy access
    Then the response status is 200
    And access is granted

  Scenario: Remy is inaccessible when user not on access list
    Given I am authenticated as a viewer in org "acme"
    And the Remy access list does not include my role or user_id
    When I check remy access
    Then the response status is 403
    And access is denied

  Scenario: Remy is inaccessible when no API key configured
    Given I am authenticated as an admin in org "acme"
    And no model backends exist for the org
    When I check remy access
    Then the response status is 403
    And the error indicates no API key configured
