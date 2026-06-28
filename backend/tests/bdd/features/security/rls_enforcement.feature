Feature: Row-Level Security Enforcement
  As a platform operator
  I want tenant isolation enforced at the database layer
  So that organisations can only access their own data

  Scenario: User can only see their own org's pipelines
    Given org "acme" has pipelines "deploy-service, nightly-report"
    And I am authenticated in org "acme"
    When the service accesses pipelines as user in org "acme"
    Then the response status is 200
    And the response contains 2 pipelines

  Scenario: Cross-org pipeline access returns 404
    Given org "acme" has pipeline "deploy-service"
    And I am authenticated in org "other-org"
    When the service accesses pipeline deploy-service as user in org "other-org"
    Then the response status is 404

  Scenario: Unauthenticated request returns 401
    Given org "acme" has pipeline "deploy-service"
    When an unauthenticated request accesses pipeline deploy-service
    Then the response status is 401

  Scenario: Viewer role cannot create pipelines
    Given org "acme" has pipeline "deploy-service"
    And I am authenticated as a viewer in org "acme"
    When a viewer tries to create a pipeline named new-pipeline
    Then the response status is 403

  Scenario: RLS context requires an active transaction
    Given org "acme" has pipeline "deploy-service"
    When RLS context is set outside a transaction
    Then a RuntimeError is raised
