Feature: Team-Scoped HITL Gates
  As a team member
  I want HITL gates to be scoped to my team
  So that only my team members can approve team-critical gates

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Team member can claim team-scoped HITL gate
    Given a team "docs-team" exists
    And I am a member of team "docs-team"
    When I claim a HITL gate scoped to team "docs-team"
    Then the claim succeeds

  Scenario: Non-member cannot claim team-scoped HITL gate
    Given a team "docs-team" exists
    And I am not a member of team "docs-team"
    When I claim a HITL gate scoped to team "docs-team"
    Then the claim is rejected with 403
