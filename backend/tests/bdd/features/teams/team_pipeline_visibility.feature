Feature: Team Pipeline Visibility
  As a pipeline owner
  I want to scope pipeline visibility to a specific team
  So that only team members can see and operate the pipeline

  Scenario: Create a pipeline with team visibility
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    When I create a pipeline named "deploy-pipeline" with visibility "team" owned by team "engineering"
    Then the response status is 201
    And the pipeline has visibility "team"

  Scenario: Team member sees team-scoped pipeline in list
    Given a team "engineering" exists
    And a pipeline "secret-pipeline" is owned by team "engineering" with visibility "team"
    And user "alice" is a member of team "engineering"
    When user "alice" requests the pipeline list
    Then the response contains pipeline "secret-pipeline"

  Scenario: Non-member cannot see team-scoped pipeline
    Given a team "engineering" exists
    And a pipeline "secret-pipeline" is owned by team "engineering" with visibility "team"
    And user "bob" is not a member of team "engineering"
    When user "bob" requests the pipeline list
    Then the response does not contain pipeline "secret-pipeline"

  Scenario: Non-member gets 404 when accessing team pipeline directly
    Given a team "engineering" exists
    And a pipeline "secret-pipeline" is owned by team "engineering" with visibility "team"
    And user "bob" is not a member of team "engineering"
    When user "bob" requests GET /api/pipelines/secret-pipeline
    Then the response status is 404

  Scenario: Admin can see all team pipelines
    Given a team "engineering" exists
    And a pipeline "secret-pipeline" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I request the pipeline list
    Then the response contains pipeline "secret-pipeline"

  Scenario: Team operator can edit team pipeline
    Given a team "engineering" exists
    And a pipeline "deploy-pipeline" is owned by team "engineering" with visibility "team"
    And I am authenticated as a team operator of team "engineering"
    When I update pipeline "deploy-pipeline" with new name "deploy-v2"
    Then the response status is 200

  Scenario: Change pipeline visibility from team to org
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And a pipeline "shared-pipeline" is owned by team "engineering" with visibility "team"
    When I update pipeline "shared-pipeline" visibility to "org"
    Then the response status is 200
    And the pipeline visibility is "org"

