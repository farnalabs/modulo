Feature: Schema Creation
  As a pipeline author
  I want to create JSON Schema definitions
  So that agent inputs and outputs are validated against a known structure

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a simple schema
    When I POST /api/schemas with name "review-input" and valid JSON Schema
    Then the response status is 201
    And the response contains id and name

  Scenario: Create a schema with nested fields
    When I POST /api/schemas with name "nested-schema" and nested JSON Schema
    Then the response status is 201
    And the schema has nested properties

  Scenario: Schema name must be unique within org
    Given org "acme" has schema "review-input"
    When I POST /api/schemas with name "review-input"
    Then the response status is 409

  Scenario: Invalid JSON Schema is rejected
    When I POST /api/schemas with name "bad-schema" and invalid JSON Schema
    Then the response status is 422
    And the error describes the schema validation failure

  Scenario: Schema belongs to the creating org
    Given I POST /api/schemas with name "my-schema" and valid JSON Schema
    When I authenticate as a user in "othercorp"
    And I GET /api/schemas/my-schema
    Then the response status is 404
