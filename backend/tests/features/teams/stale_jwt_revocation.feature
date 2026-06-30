Feature: Stale JWT Team Membership Revocation
  As an org admin
  I want membership changes to take effect immediately for revoked users
  So that a removed team member cannot continue accessing team resources

  Scenario: Immediate revocation invalidates active tokens
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And user "alice" is a member of team "engineering"
    And user "alice" holds a valid JWT
    When I revoke user "alice"'s session
    Then user "alice" is redirected to re-authenticate on next request

  Scenario: Removed member cannot access team resources after token refresh
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And user "alice" is a member of team "engineering"
    And a pipeline "eng-pipeline" is owned by team "engineering" with visibility "team"
    When I remove user "alice" from team "engineering"
    And user "alice" refreshes their JWT
    And user "alice" requests GET /api/pipelines/eng-pipeline
    Then the response status is 404

  Scenario: Stale JWT grace period is documented
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And user "alice" is a member of team "engineering"
    When I change user "alice"'s role from "operator" to "viewer"
    And user "alice" uses an unexpired JWT issued before the change
    Then the response respects the old role until token refresh
    But this is a documented acceptable gap of up to 15 minutes

  Scenario: HITL gate bypasses stale JWT with DB-live check
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And user "alice" is a member of team "engineering" with role "operator"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "engineering"
    And user "alice" is removed from team "engineering"
    But user "alice" still holds a valid JWT
    When user "alice" attempts to claim gate "gate-1" on run "run-1"
    Then the response status is 403
    And the HITL gate enforcement uses a DB-live membership check

