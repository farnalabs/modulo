Feature: Team Creation
  As an admin
  I want to create teams with valid names
  So that I can organise users and resources

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a team with valid name
    When I create a team with name "docs-team" and description "Documentation pipelines"
    Then the response status is 201
    And the response contains a team with name "docs-team"
    And the team has an account_id

  Scenario: Create a team with empty name is rejected
    When I create a team with name "" and description "empty"
    Then the response status is 422

  Scenario: Create a team with duplicate name is rejected
    Given a team "docs-team" exists
    When I create a team with name "docs-team" and description "duplicate"
    Then the response status is 409
