Feature: Team Deletion Blocked When Resources Exist
  As an org admin
  I want to be prevented from deleting a team that still owns resources
  So that orphans are not accidentally created

  Scenario: Delete team with owned pipelines is blocked
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And a pipeline "eng-pipeline" is owned by team "engineering"
    When I delete the team "engineering"
    Then the response status is 409
    And the error indicates the team still has resources

  Scenario: Delete team with owned connectors is blocked
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And connector "eng-connector" is owned by team "engineering"
    When I delete the team "engineering"
    Then the response status is 409

  Scenario: Delete team with owned stages is blocked
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And stage "eng-stage" is owned by team "engineering"
    When I delete the team "engineering"
    Then the response status is 409

  Scenario: Delete team with owned model backends is blocked
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And model backend "eng-backend" is owned by team "engineering"
    When I delete the team "engineering"
    Then the response status is 409

  Scenario: Reassign resources then delete succeeds
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And a pipeline "eng-pipeline" is owned by team "engineering"
    When I reassign all resources from team "engineering" to org-wide
    And I delete the team "engineering"
    Then the response status is 204

  Scenario: Error lists resource types blocking deletion
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And a pipeline "eng-pipeline" is owned by team "engineering"
    And connector "eng-connector" is owned by team "engineering"
    When I delete the team "engineering"
    Then the error message contains "pipeline"
    And the error message contains "connector"

  Scenario: Non-admin cannot delete team even with no resources
    Given I am authenticated as a viewer in org "acme"
    And a team "engineering" exists
    And the team has no resources
    When I delete the team "engineering"
    Then the response status is 403

