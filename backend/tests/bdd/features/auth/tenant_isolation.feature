Feature: Tenant Isolation
  As the platform
  I want to ensure each organisation can only see its own data
  So that no data leaks between tenants

  Scenario: Organisation A cannot see Organisation B's pipelines
    Given organisation "acme" has pipeline "deploy-service"
    And organisation "globex" has pipeline "run-tests"
    When I authenticate as a user in "acme"
    And I GET /api/pipelines
    Then I see "deploy-service"
    And I do not see "run-tests"

  Scenario: RLS is enforced at the database layer
    Given organisation "acme" has pipeline "private-pipeline"
    When a raw query runs without setting app.current_org_id
    Then the query returns no rows for "private-pipeline"

  Scenario: Cross-org pipeline run is forbidden
    Given organisation "acme" has pipeline "acme-pipe"
    And I authenticate as a user in "globex"
    When I POST /api/pipelines/acme-pipe/runs
    Then the response status is 404
