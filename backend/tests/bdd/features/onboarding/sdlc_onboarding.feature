Feature: SDLC Onboarding Path
  As a new user
  I want to complete the SDLC onboarding wizard
  So that I can connect tools, infer schemas, and wire my first pipeline

  Background:
    Given the SDLC onboarding steps are configured

  Scenario: Full SDLC onboarding flow — first run
    Given no steps have been completed yet
    When I GET /api/v1/onboarding/status
    Then the response indicates it is the first run
    And the current step is step 1
    And the total steps is 5

  Scenario: Connect tools step shows available connectors
    Given no steps have been completed yet
    When I GET /api/v1/onboarding/step/connect_tools
    Then the response contains connector options for "github", "jira", and "linear"
    And marking "connect_tools" as completed advances to step 2

  Scenario: Run inference against a connector
    Given I have a connector instance with sample data
    When I POST /api/v1/schemas/infer with the connector instance
    Then the response contains a definition_json
    And I mark "run_inference" as completed
    Then marking "run_inference" as completed advances to step 3

  Scenario: Review and publish inferred schemas
    Given I have an inferred draft schema
    And I have completed the review_schemas step
    When I publish the schema via POST /api/v1/schemas with version "1.0.0"
    Then the schema version is published

  Scenario: Browse library filtered by abstract_name
    Given a published schema with abstract_name "issue-tracker"
    When I GET /api/v1/library/browse?q=issue-tracker
    Then the response contains relevant library primitives

  Scenario: Wire pipeline — select template and complete
    Given I have completed "browse_library" and "wire_pipeline"
    When I select a pipeline template and mark "wire_pipeline" as completed
    Then all 5 SDLC onboarding steps are completed
    And is_first_run becomes false

  Scenario: Re-run inference
    Given an inference result already exists
    When I POST /api/v1/schemas/infer again with updated sample data
    Then a new definition_json is returned
    And the existing inference is replaced

  Scenario: State persistence — completed steps persist between requests
    Given no steps have been completed yet
    When I make a new GET /api/v1/onboarding/status request
    Then the response shows 0 completed steps
