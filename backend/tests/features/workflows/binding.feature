Feature: Binding wizard for imported workflows
  When importing a workflow, the binding wizard maps exported references
  (connector types, abstract schemas, model backends) to local equivalents.

  Background:
    Given the organisation has a "filesystem" connector instance
    And has a model backend "claude-sonnet-4"
    And has a schema "PRD Input Schema" with abstract_name "prd-input"

  Scenario: Connector type resolved to local instance
    Given the bundle references connector type "filesystem"
    When the import analysis resolves connectors
    Then resolved_connectors contains a match with instance_id and instance_name
    And no warning is emitted for this connector type

  Scenario: Unmatched connector type generates warning
    Given the bundle references connector type "slack"
    And no "slack" connector instance exists
    When the import analysis resolves connectors
    Then resolved_connectors contains a warning
    And the warning mentions "slack"

  Scenario: Schema resolved by abstract_name
    Given the bundle references a schema with abstract_name "prd-input"
    When the import analysis resolves schemas
    Then the schema is matched to the local schema by abstract_name
    And the resolved schema has schema_id set

  Scenario: Schema resolved by definition structure
    Given the bundle references a schema matching the structure of "Requirements Output Schema"
    When the import analysis resolves schemas
    Then the schema is matched by definition_json equality
    And the resolved schema has schema_id set

  Scenario: Model backend resolved by name
    Given the bundle includes model_backend "claude-sonnet-4"
    When the import analysis resolves model backends
    Then the model backend is matched by name
    And resolved_model_backends has model_backend_id set

  Scenario: Model backend resolved by provider+model_id fallback
    Given the bundle includes model_backend with provider "anthropic" and model_id "claude-sonnet-4-20241022"
    And no backend exists with that exact name
    When the import analysis resolves model backends
    Then the model backend is matched by provider+model_id
    And resolved_model_backends has model_backend_id set
