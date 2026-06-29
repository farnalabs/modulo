Feature: Schema Inference
  As a pipeline author
  I want to infer JSON Schema drafts from connected tool sample data
  So that I can define structured schemas without manual authoring

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Infer returns draft schema from connector sample data
    Given a connector instance "github-issues" with sample data
    And a model backend is configured
    When I POST /api/v1/schemas/infer with the connector instance
    Then the response status is 200
    And the response contains definition_json
    And the response contains sample_count and suggestion_name

  Scenario: Field types are detected from sample records
    Given a connector with mixed-type sample records
    When I POST /api/v1/schemas/infer with the connector instance
    Then the response status is 200
    And the inferred schema has "string" type for field "title"
    And the inferred schema has "number" type for field "priority"
    And the inferred schema has "boolean" type for field "completed"
    And the inferred schema has "array" type for field "tags"

  Scenario: Enum values are suggested for constrained fields
    Given sample records with a field having few distinct values
    When I POST /api/v1/schemas/infer with the connector instance
    Then the response status is 200
    And the inferred schema includes an enum constraint for field "status"

  Scenario: Default sample limit is applied when omitted
    Given a connector instance "github-issues" with sample data
    And a model backend is configured
    When I POST /api/v1/schemas/infer with the connector instance and no limit
    Then the response status is 200
    And the sample query limit defaults to 10

  Scenario: Select connector instance by UUID
    Given a connector instance "jira-tasks" with sample data
    And a model backend is configured
    When I POST /api/v1/schemas/infer with connector id "jira-tasks"
    Then the response status is 200
    And the suggestion name mentions "jira-tasks"

  Scenario: Publish inferred schema as a schema version
    Given a connector instance "github-issues" with sample data
    And a model backend is configured
    When I infer a schema from the connector
    And I create a schema "inferred-schema" from the draft
    And I publish version "1.0" of the schema with the inferred definition
    Then the response status is 201
    And the schema version is published
