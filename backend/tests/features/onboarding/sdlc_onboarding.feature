Feature: SDLC Onboarding Path
  As a new Modulo user with existing SDLC tooling
  I want to complete a guided 5-step onboarding wizard
  So that I can connect my tools, infer schemas, and wire my first pipeline

  Background:
    Given the SDLC onboarding steps are configured
    And no steps have been completed yet

  Scenario: Full SDLC onboarding flow
    When I GET /api/v1/onboarding/status
    Then the response indicates it is the first run
    And the current step is step 1
    And the total steps is 5

  Scenario: Connect tools step shows available connectors
    When I GET /api/v1/onboarding/step/connect_tools
    Then the response contains connector options for "github", "jira", and "linear"
    And marking "connect_tools" as completed advances to step 2

  Scenario: Run inference on connected connector
    Given I have completed the connect_tools step
    And I have a connector instance with sample data
    When I POST /api/v1/schemas/infer with the connector instance
    Then the response status is 200
    And the response contains a definition_json
    And I mark "run_inference" as completed

  Scenario: Review and publish inferred schemas
    Given I have completed the run_inference step
    And I have an inferred draft schema
    When I publish the schema via POST /api/v1/schemas with version "1.0"
    Then the response status is 201
    And the schema version is published
    And I mark "review_schemas" as completed

  Scenario: Browse library filtered by inferred abstract name
    Given I have completed the review_schemas step
    And a published schema with abstract_name "issue-tracker"
    When I GET /api/v1/library/browse?q=issue-tracker
    Then the response contains relevant library primitives
    And I mark "browse_library" as completed

  Scenario: Wire pipeline completes onboarding
    Given I have completed the browse_library step
    When I select a pipeline template and mark "wire_pipeline" as completed
    Then all 5 SDLC onboarding steps are completed
    And is_first_run becomes false

  Scenario: Re-run inference after connector data changes
    Given I have completed the connect_tools step
    And an inference result already exists
    When I POST /api/v1/schemas/infer again with updated sample data
    Then a new definition_json is returned
    And the existing inference is replaced

  Scenario: Onboarding state is persisted across sessions
    Given I have completed "connect_tools" and "run_inference"
    When I make a new GET /api/v1/onboarding/status request
    Then the response shows 2 completed steps
    And the current step is step 3
