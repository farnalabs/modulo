Feature: Pipeline CRUD
  As a pipeline author
  I want to create, read, update and delete pipelines
  So that I can manage my team's agentic workflows

  Scenario: Create a pipeline
    Given I am authenticated as an admin in org "acme"
    When I POST /api/pipelines with name "my-pipeline" and valid config
    Then the response status is 201
    And the response contains id and slug

  Scenario: List pipelines
    Given org "acme" has pipelines "alpha", "beta", "gamma"
    And I am authenticated in org "acme"
    When I GET /api/pipelines
    Then the response contains 3 pipelines

  Scenario: Get pipeline by id
    Given org "acme" has pipeline "alpha" with id "11111111-2222-3333-4444-555555555555"
    And I am authenticated in org "acme"
    When I GET /api/pipelines/11111111-2222-3333-4444-555555555555
    Then the response status is 200
    And the response name is "alpha"

  Scenario: Update pipeline config
    Given org "acme" has pipeline "alpha"
    And I am authenticated as an admin in org "acme"
    When I PATCH /api/pipelines/alpha with new config
    Then the response status is 200

  Scenario: Delete pipeline
    Given org "acme" has pipeline "obsolete"
    And I am authenticated as an admin in org "acme"
    When I DELETE /api/pipelines/obsolete
    Then the response status is 204
    And the pipeline no longer exists

  Scenario: Cloning a pipeline records an audit event
    Given org "acme" has pipeline "my-pipeline"
    When I clone pipeline "my-pipeline"
    Then the response status is 201
    And a clone audit event is recorded
