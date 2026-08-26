Feature: ViewModel Current
  As a tenant user
  I want a single aggregate endpoint describing my current workspace
  So that the frontend can render user, org, plan, pipeline, and approval state in one request

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Returns user identity and org context
    Given the organisation "acme" is named "Acme Org" with daily spend limit 50.0
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the current user is "testuser" with org role "admin"
    And the current org is named "Acme Org"

  Scenario: Returns team memberships and preferences
    Given the account has preferences {"theme": "dark", "notifications": true}
    And I hold a workspace membership with role "operator"
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the response includes a team membership with role "operator"
    And the response includes the account preferences

  Scenario: Returns feature flags with active status
    Given the plan enables the features "parallel_branches" and "eval_system"
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the response includes the enabled feature flags

  Scenario: Returns plan info with daily spend limit
    Given the organisation "acme" is named "Acme Org" with daily spend limit 50.0
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the plan tier is "team" with a daily spend limit of 50.0

  Scenario: Returns pipelines and recent runs with totals
    Given pipeline "release-pipeline" is visible in the org
    And a recent run for "release-pipeline" exists
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the response lists pipeline "release-pipeline" with 1 recent run

  Scenario: Returns pending approval gates
    Given pipeline "release-pipeline" is visible in the org
    And a pending approval gate exists for "release-pipeline"
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the response includes 1 pending approval gate

  Scenario: Returns saved views
    Given a saved view "Deployments" of type "run_list" exists
    When I GET /api/v1/viewmodel/current
    Then the response status is 200
    And the response includes a saved view named "Deployments"

  Scenario: Returns the selected saved view as the current view
    Given a saved view "Deployments" of type "run_list" exists
    And I select the saved view "Deployments"
    When I GET /api/v1/viewmodel/current with the selected view
    Then the response status is 200
    And the response includes the selected current view named "Deployments"

  Scenario: Non-admin cannot use view_as_team
    Given I am authenticated as a viewer in org "acme"
    When I GET /api/v1/viewmodel/current with view_as_team
    Then the response status is 403

  Scenario: Unknown view_as_team team returns 404
    When I GET /api/v1/viewmodel/current with unknown view_as_team
    Then the response status is 404

  Scenario: Unauthenticated request is rejected
    When I GET /api/v1/viewmodel/current without authentication
    Then the response status is 401

  Scenario: Missing organisation returns 404
    Given the organisation does not exist
    When I GET /api/v1/viewmodel/current
    Then the response status is 404

  Scenario: Missing account returns 404
    Given the account does not exist
    When I GET /api/v1/viewmodel/current
    Then the response status is 404

  Scenario: Programming error is mapped to 501
    Given the organisation lookup fails with a programming error
    When I GET /api/v1/viewmodel/current
    Then the response status is 501

  Scenario: Database failure is mapped to 503
    Given the organisation lookup fails with a database error
    When I GET /api/v1/viewmodel/current
    Then the response status is 503
