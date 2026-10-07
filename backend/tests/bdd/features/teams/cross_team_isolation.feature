Feature: Cross-Team Isolation
  As an org admin
  I want teams to be isolated from each other's resources
  So that one team cannot access or enumerate another team's private resources

  Scenario: Team A cannot see Team B's team-scoped pipeline
    Given a team "engineering" exists
    And a team "design" exists
    And a pipeline "eng-pipeline" is owned by team "engineering" with visibility "team"
    And user "alice" is a member of team "design"
    When user "alice" requests the pipeline list
    Then the response does not contain pipeline "eng-pipeline"

  Scenario: Team A cannot access Team B's connector
    Given a team "engineering" exists
    And a team "design" exists
    And connector "design-connector" is owned by team "design" with visibility "team"
    And user "alice" is a member of team "engineering"
    When user "alice" requests GET /api/connectors/design-connector
    Then the response status is 404

  Scenario: Cross-team pipeline binding is blocked
    Given a team "engineering" exists
    And a team "design" exists
    And a pipeline "design-pipeline" is owned by team "design" with visibility "team"
    And connector "eng-connector" is owned by team "engineering" with visibility "team"
    And I am authenticated as an admin in org "acme"
    When I bind connector "eng-connector" to a node in pipeline "design-pipeline"
    Then the response status is 409
    And the error indicates connector_team_mismatch

  Scenario: Org-wide resources are accessible across teams
    Given a team "engineering" exists
    And a team "design" exists
    And connector "shared-connector" has visibility "org"
    And user "alice" is a member of team "engineering"
    And user "bob" is a member of team "design"
    When user "alice" requests GET /api/connectors/shared-connector
    Then the response status is 200
    When user "bob" requests GET /api/connectors/shared-connector
    Then the response status is 200

  Scenario: No "N hidden" enumeration leak
    Given a team "engineering" exists
    And a team "design" exists
    And a pipeline "eng-pipeline" is owned by team "engineering" with visibility "team"
    And user "alice" is a member of team "design"
    When user "alice" requests the pipeline list
    Then the response total count does not include team-private pipelines

  # Lifecycle-map team isolation is deliberately NOT asserted in this feature:
  # the step definitions here filter an in-memory dict with no database behind
  # it, so such a scenario passes even with the whole change reverted. The real
  # coverage is:
  #   * tests/integration/test_rls_isolation.py::
  #     test_lifecycle_and_eval_tables_team_rls_enforcement — the DB policy:
  #     a member sees team + org rows, a cross-team non-member sees only the
  #     org row, the execution context sees both;
  #   * tests/unit/api/test_lifecycle_maps_routes.py — the request-time route
  #     gate: non-member 403, member 200, non-admin restore 200.
