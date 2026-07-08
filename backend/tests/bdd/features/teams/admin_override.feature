Feature: Admin Override of Team Restrictions
  As an org admin
  I want to bypass team visibility restrictions
  So that I can manage all resources across the org

  Scenario: Admin can see all team-private pipelines
    Given team "engineering" exists
    And team "design" exists
    And pipeline "eng-pipeline" is owned by team "engineering" with visibility "team"
    And pipeline "des-pipeline" is owned by team "design" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I request the pipeline list
    Then the response contains pipeline "eng-pipeline"
    And the response contains pipeline "des-pipeline"

  Scenario: Admin can access team-private connector
    Given team "engineering" exists
    And connector "eng-connector" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I request GET /api/connectors/eng-connector
    Then the response status is 200

  Scenario: Admin can delete a team-scoped resource
    Given team "engineering" exists
    And a pipeline "old-pipeline" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I delete pipeline "old-pipeline"
    Then the response status is 204

  Scenario: Admin can reassign team ownership
    Given team "engineering" exists
    And team "design" exists
    And pipeline "movable-pipeline" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I reassign pipeline "movable-pipeline" to team "design"
    Then the response status is 200
    And the pipeline owner is team "design"

  Scenario: Admin can bulk reassign team resources
    Given team "engineering" exists
    And pipeline "p1" is owned by team "engineering" with visibility "team"
    And pipeline "p2" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I bulk reassign all resources from team "engineering" to org-wide
    Then the response status is 200
    And pipeline "p1" has owner_team_id null
    And pipeline "p2" has owner_team_id null

  Scenario: Non-admin cannot override team visibility
    Given team "engineering" exists
    And pipeline "secret-pipeline" is owned by team "engineering" with visibility "team"
    And I am authenticated as a viewer in org "acme"
    When I request GET /api/pipelines/secret-pipeline
    Then the response status is 404

