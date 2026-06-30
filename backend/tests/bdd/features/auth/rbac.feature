Feature: Role-Based Access Control
  As the platform
  I want to enforce role hierarchy across org and team scopes
  So that users only have the access they are entitled to

  Background:
    Given I am authenticated as an admin in org "acme"

  # ── Privilege cap model (unit-level) ─────────────────────────────

  Scenario: Effective team role is capped by org role
    Given I am an admin user with org role "runner"
    And I have team role "operator"
    When I compute the effective team role
    Then the effective role is "runner"

  Scenario: Viewer org role cannot be elevated by team role
    Given I am an admin user with org role "viewer"
    And I have team role "operator"
    When I compute the effective team role
    Then the effective role is "viewer"

  Scenario: Admin org role allows any team role
    Given I am an admin user with org role "admin"
    And I have team role "runner"
    When I compute the effective team role
    Then the effective role is "runner"

  Scenario: Unrecognised role falls back to viewer
    Given I am an admin user with org role "superadmin"
    And I have team role "operator"
    When I compute the effective team role
    Then the effective role is "viewer"

  Scenario: Role hierarchy levels are ordered correctly
    Given the role hierarchy for "admin" is 3
    And the role hierarchy for "operator" is 2
    And the role hierarchy for "runner" is 1
    And the role hierarchy for "viewer" is 0
    Then each level is strictly higher than the previous

  # ── Team CRUD ──────────────────────────────────────────────────

  Scenario: Admin creates a team
    Given a team "docs-team" does not exist
    When I create a team with name "docs-team" and description "Documentation pipelines"
    Then the response status is 201
    And the response contains a team with name "docs-team"
    And the team has an account_id

  Scenario: Admin can list teams
    And a team "docs-team" exists
    When I list teams
    Then the response status is 200
    And the response contains a list of teams

  Scenario: Admin can get a team by ID
    And a team "docs-team" exists
    When I get team "docs-team"
    Then the response status is 200
    And the response contains a team with name "docs-team"

  Scenario: Non-admin cannot create a team
    Given I am authenticated as a viewer in org "acme"
    When I create a team with name "docs-team" and description "x"
    Then the response status is 403

  Scenario: Duplicate team name is rejected
    And a team "docs-team" exists
    When I create a team with name "docs-team" and description "duplicate"
    Then the response status is 409

  Scenario: Empty team name is rejected
    When I create a team with name "" and description "empty"
    Then the response status is 422

  Scenario: Fetching a non-existent team returns 404
    When I get team by id "00000000-0000-0000-0000-000000000099"
    Then the response status is 404

  Scenario: Deleting a non-existent team returns 404
    When I delete team by id "00000000-0000-0000-0000-000000000099"
    Then the response status is 404

  Scenario: Admin can rename a team
    And a team "docs-team" exists
    When I rename team "docs-team" to "documentation-team"
    Then the response status is 200
    And the response contains a team with name "documentation-team"

  Scenario: Renaming to an already-taken name returns 409
    And a team "docs-team" exists
    And a team "legal-team" exists
    When I rename team "docs-team" to "legal-team"
    Then the response status is 409

  Scenario: Admin can delete an empty team
    And a team "docs-team" exists
    Given the team has no resources
    When I delete the team "docs-team"
    Then the response status is 204

  # ── Team membership ─────────────────────────────────────────────

  Scenario: Admin can add a user to a team
    And a team "docs-team" exists
    And a user "alice" exists
    When I add user "alice" to team "docs-team" with role "viewer"
    Then the response status is 201

  Scenario: Admin can remove a user from a team
    And a team "docs-team" exists
    And a user "alice" exists
    And user "alice" is already a member of team "docs-team"
    When I remove user "alice" from team "docs-team"
    Then the response status is 200

  Scenario: Adding user to non-existent team returns 404
    And a user "alice" exists
    When I add user "alice" to team "nonexistent" with role "viewer"
    Then the response status is 404

  Scenario: Adding user with role exceeding org role is rejected
    And a team "docs-team" exists
    And a user "alice" exists with org role "viewer"
    When I add user "alice" to team "docs-team" with role "operator"
    Then the response status is 422

  Scenario: Duplicate membership is rejected
    And a team "docs-team" exists
    And a user "alice" exists
    And user "alice" is already a member of team "docs-team"
    When I add user "alice" to team "docs-team" with role "viewer"
    Then the error indicates user is already a member

  # ── Feature gating ─────────────────────────────────────────────

  Scenario: Team RBAC is gated behind feature flag
    Given I do not have a Team license
    When I GET /api/v1/teams
    Then the response status is 402
    And the error detail mentions "team_rbac"

  # ── Team deletion with resources ───────────────────────────────

  Scenario: Team with resources cannot be deleted
    And a team "docs-team" exists
    And a pipeline "review-pipeline" is owned by team "docs-team"
    When I delete the team "docs-team"
    Then the response status is 409
    And the error indicates the team still has resources

  Scenario: Team with no resources can be deleted
    And a team "docs-team" exists
    Given the team has no resources
    When I delete the team "docs-team"
    Then the response status is 204
