Feature: Schema Inference
  As a pipeline author
  I want to infer a JSON Schema from connector sample data
  So that I can define structured schemas without manual authoring

  Scenario: Infer schema from connector sample data
    Given a connector instance with sample data
    And a model backend is configured
    When I POST /api/schemas/infer with the connector instance
    Then the response status is 200
    And the response contains a definition_json
    And the response has a suggestion_name

  Scenario: Schema inference fails when connector not found
    Given a non-existent connector instance
    When I POST /api/schemas/infer with the connector instance
    Then the response status is 404

  Scenario: Schema inference fails without model backend
    Given a connector instance with sample data
    And no model backends are configured
    When I POST /api/schemas/infer with the connector instance
    Then the response status is 400

  Scenario: Generated schema validates against Draft 2020-12
    Given a generated schema definition
    When I validate the schema
    Then the schema is structurally valid

  Scenario: Schema migration plan is computed between versions
    Given a source schema and a target schema
    When I POST /api/schemas/migrate/plan with both definitions
    Then the response contains field_additions and field_removals
