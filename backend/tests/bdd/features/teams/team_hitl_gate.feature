Feature: Team-Scoped HITL Gates
  As a pipeline author
  I want to require a specific team to approve HITL gates
  So that only the right team reviews sensitive decisions

  Scenario: HITL gate with required_team_id is claimable by team members only
    Given a team "engineering" exists
    And user "alice" is a member of team "engineering" with role "operator"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "engineering"
    When user "alice" claims the HITL gate "gate-1" on run "run-1"
    Then the response status is 200
    And the response contains a claim_token

  Scenario: Non-member cannot claim team-required HITL gate
    Given a team "engineering" exists
    And user "bob" is not a member of team "engineering"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "engineering"
    When user "bob" claims the HITL gate "gate-1" on run "run-1"
    Then the response status is 403
    And the error indicates the gate requires team "engineering"

  Scenario: MCP client cannot approve human_only + required_team gate
    Given a team "engineering" exists
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "engineering" and human_only true
    And user "alice" is a member of team "engineering" with role "operator"
    When an MCP client attempts to approve gate "gate-1" on run "run-1" as user "alice"
    Then the response status is 403
    And the error indicates the gate requires human approval

  Scenario: Gate context exposes required_team_id
    Given a team "engineering" exists
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "engineering"
    When I request the gate context for run "run-1" gate "gate-1"
    Then the response contains required_team_id "engineering"
    And the response contains required_team_name "engineering"

  Scenario: Team operator can approve team HITL gate
    Given a team "engineering" exists
    And user "alice" is a member of team "engineering" with role "operator"
    And a run "run-1" is awaiting human at gate "gate-1" with required_team_id "engineering"
    And user "alice" holds a valid claim_token for gate "gate-1"
    When user "alice" approves gate "gate-1" on run "run-1"
    Then the response status is 200
    And the run resumes execution

