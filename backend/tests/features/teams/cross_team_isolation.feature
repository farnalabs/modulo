Feature: Cross-Team Isolation
  As an org admin
  I want teams to be isolated from each other's resources
  So that one team cannot access or enumerate another team's private resources

  Scenario: Team A cannot see Team B's team-scoped pipeline
    Given team "engineering" exists
    And team "design" exists
    And pipeline "eng-pipeline" is owned by team "engineering" with visibility "team"
    And user "alice" is a member of team "design"
    When user "alice" requests the pipeline list
    Then the response does not contain pipeline "eng-pipeline"

  Scenario: Team A cannot access Team B's connector
    Given team "engineering" exists
    And team "design" exists
    And connector "design-connector" is owned by team "design" with visibility "team"
    And user "alice" is a member of team "engineering"
    When user "alice" requests GET /api/connectors/design-connector
    Then the response status is 404

  Scenario: Cross-team pipeline binding is blocked
    Given team "engineering" exists
    And team "design" exists
    And pipeline "design-pipeline" is owned by team "design" with visibility "team"
    And connector "eng-connector" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I bind connector "eng-connector" to a node in pipeline "design-pipeline"
    Then the response status is 409
    And the error indicates connector_team_mismatch

  Scenario: Org-wide resources are accessible across teams
    Given team "engineering" exists
    And team "design" exists
    And connector "shared-connector" has visibility "org"
    And user "alice" is a member of team "engineering"
    And user "bob" is a member of team "design"
    When user "alice" requests GET /api/connectors/shared-connector
    Then the response status is 200
    When user "bob" requests GET /api/connectors/shared-connector
    Then the response status is 200

  Scenario: No "N hidden" enumeration leak
    Given team "engineering" exists
    And team "design" exists
    And pipeline "eng-pipeline" is owned by team "engineering" with visibility "team"
    And user "alice" is a member of team "design"
    When user "alice" requests the pipeline list
    Then the response total count does not include team-private pipelines

