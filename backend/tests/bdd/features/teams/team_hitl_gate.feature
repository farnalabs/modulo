Feature: Team-Scoped HITL Gates
  As a team member
  I want HITL gates to be scoped to my team
  So that only my team members can approve team-critical gates

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Team member can claim team-scoped HITL gate
    Given a team "docs-team" exists
    And user "admin" is a member of team "docs-team" with role "operator"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "docs-team"
    When user "admin" claims the HITL gate "gate-1" on run "run-1"
    Then the response status is 200
    And the response contains a claim_token

  Scenario: Non-member cannot claim team-scoped HITL gate
    Given a team "docs-team" exists
    And user "admin" is not a member of team "docs-team"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "docs-team"
    When user "admin" claims the HITL gate "gate-1" on run "run-1"
    Then the response status is 403
    And the error indicates the gate requires team "docs-team"

  Scenario: Team member can approve team-scoped HITL gate
    Given a team "docs-team" exists
    And user "admin" is a member of team "docs-team" with role "operator"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "docs-team"
    And user "admin" holds a valid claim_token for gate "gate-1"
    When user "admin" approves gate "gate-1" on run "run-1"
    Then the response status is 200
    And the run resumes execution

  Scenario: Gate context exposes required_team_id and required_team_name
    Given a team "docs-team" exists
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "docs-team"
    When I request the gate context for run "run-1" gate "gate-1"
    Then the response status is 200
    And the response contains required_team_id "docs-team"
    And the response contains required_team_name "docs-team"
