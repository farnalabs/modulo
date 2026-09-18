Feature: View as Team
  As an org admin
  I want to temporarily scope my view to a specific team
  So that I can inspect what resources are visible to that team

  Scenario: Admin views resources as a team
    Given a team "engineering" exists
    And I am authenticated as an admin in org "acme"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 200
    And the response contains only team-scoped resources for "engineering"

  Scenario: Resources are filtered to team scope
    Given a team "engineering" exists
    And a team "design" exists
    And pipeline "release-pipeline" is owned by team "engineering"
    And pipeline "brand-pipeline" is owned by team "design"
    And I am authenticated as an admin in org "acme"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 200
    And the response contains pipeline "release-pipeline"
    And the response does not contain pipeline "brand-pipeline"

  Scenario: Non-admin cannot use view_as_team
    Given a team "engineering" exists
    And I am authenticated as a viewer in org "acme"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

  Scenario: Invalid team returns 404
    Given I am authenticated as an admin in org "acme"
    When I GET /api/viewmodel/current with view_as_team "nonexistent"
    Then the response status is 404

  Scenario: Admin restores to org-wide view
    Given a team "engineering" exists
    And pipeline "release-pipeline" is owned by team "engineering"
    And I am authenticated as an admin in org "acme"
    When I GET /api/viewmodel/current without view_as_team
    Then the response status is 200
    And the response contains pipeline "release-pipeline"
