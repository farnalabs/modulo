Feature: Composite Library
  As a pipeline author
  I want to save composite templates as library primitives and browse them
  So that I can discover and reuse composite patterns

  Background:
    Given the organisation has 2 composite library primitives

  Scenario: Save composite as library primitive
    Given a composite template "review-composite" exists
    When the user saves the composite template as a library primitive with type "composite"
    Then the response status is 201
    And the library primitive has primitive_type "composite"
    And the library primitive content_json matches the composite template

  Scenario: Browse composites in library filtered by type
    When the user requests GET /api/v1/libraries?primitive_type=composite
    Then the response contains only composite-type primitives
    And at least 1 composite is returned

  Scenario: Copy composite primitive (copy-to-adapt)
    Given a community composite primitive exists with id "00000000-0000-0000-0000-000000000010"
    When the user sends POST /api/v1/libraries/00000000-0000-0000-0000-000000000010/adapt
    Then the response status is 201
    And the new primitive has source "local"
    And the new primitive has forked_from set to the community primitive id

  Scenario: Composite content_json validation — missing required fields returns error
    When the user creates a library primitive with primitive_type "composite" and empty content_json
    Then the response status is 422

  Scenario: Save composite from pipeline via save-as-composite
    Given org "acme" has pipeline "my-pipeline" with agent "critic" using {{parameter.tone}}
    When the user saves the pipeline as composite with name "critic-composite" and selected node "critic"
    Then the response status is 201
    And the composite template has parameter_ports containing "tone"
