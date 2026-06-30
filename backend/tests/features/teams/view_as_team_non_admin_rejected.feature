Feature: View as Team — Non-Admin Rejection
  As a non-admin user
  I want my view_as_team parameter to be rejected
  So that I cannot bypass team visibility boundaries

  Scenario: Operator cannot use view_as_team
    Given a team "engineering" exists
    And I am authenticated as an operator in org "acme"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

  Scenario: Runner cannot use view_as_team
    Given a team "engineering" exists
    And I am authenticated as a runner in org "acme"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

  Scenario: Viewer cannot use view_as_team
    Given a team "engineering" exists
    And I am authenticated as a viewer in org "acme"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

  Scenario: API key with runner role cannot use view_as_team
    Given a team "engineering" exists
    And I authenticate with an API key with role "runner"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

  Scenario: API key with operator role cannot use view_as_team
    Given a team "engineering" exists
    And I authenticate with an API key with role "operator"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

  Scenario: view_as_team is silently ignored for non-admins on valid endpoints
    Given a team "engineering" exists
    And I am authenticated as an operator in org "acme"
    When I GET /api/pipelines with view_as_team "engineering"
    Then the response status is 200
    And the view_as_team parameter is ignored

  Scenario: Admin can still use view_as_team after being demoted
    Given a team "engineering" exists
    And I am authenticated as an admin in org "acme"
    And my role is changed to "operator"
    When I GET /api/viewmodel/current with view_as_team "engineering"
    Then the response status is 403

