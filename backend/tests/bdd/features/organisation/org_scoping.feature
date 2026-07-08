Feature: Organisation Scoping
  As a Modulo user
  I want all resources scoped to my organisation
  So that I never see data from other organisations

  Background:
    Given I am authenticated in org "acme"

  Scenario: List pipelines only shows own org
    Given org "acme" has pipeline "my-pipeline"
    And org "othercorp" has pipeline "secret-pipeline"
    When I GET /api/pipelines
    Then the response contains 1 pipeline
    And the response name is "my-pipeline"

  Scenario: Cross-org pipeline access is forbidden
    Given org "othercorp" has pipeline "secret-pipeline"
    When I GET /api/pipelines/secret-pipeline
    Then the response status is 404

  Scenario: Create pipeline is scoped to own org
    When I POST /api/pipelines with name "my-pipeline" and valid config
    Then the response contains id and slug
    And the pipeline belongs to org "acme"

  Scenario: Cross-org run creation is forbidden
    Given org "othercorp" has pipeline "secret-pipeline"
    And I POST /api/pipelines/secret-pipeline/runs with empty run_context
    Then the response status is 404
