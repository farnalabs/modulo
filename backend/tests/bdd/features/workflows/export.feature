Feature: Export workflow as YAML bundle
  Users can export a pipeline as a portable YAML bundle referencing
  ConnectorTypes and abstract schema names. The bundle strips org-private
  data (owner_team_id, credentials, visibility) and is optionally signed
  with Ed25519 for integrity verification.

  Background:
    Given a pipeline named "PRD to Tickets" exists
    And the pipeline has 2 agent nodes ("prd-reader", "ticket-writer") and 1 HITL gate
    And each agent references an abstract schema and a connector type
    And the pipeline has a model_backend_id and encrypted credentials

  Scenario: Export pipeline as YAML bundle
    When the user requests export of the pipeline
    Then the response status is 200
    And the response content-type is "application/x-yaml"
    And the body is valid YAML with top-level key "modulo_workflow"
    And modulo_workflow contains "name", "version", "agents", "edges", "schemas"

  Scenario: Export includes Ed25519 signature
    When the user requests export with "sign: true"
    Then the response includes a "signature" field under modulo_workflow
    And the signature is a valid Ed25519 base64-encoded string
    And the signature verifies against the Modulo registry public key

  Scenario: Exported bundle strips credentials
    When the exported YAML is inspected
    Then the agents section does not contain any credential or ciphertext fields
    And the pipeline section does not contain owner_team_id
    And the pipeline section does not contain visibility
    And model_backend_id references are preserved as abstract names

  Scenario: Export preserves all pipeline configuration
    When the exported YAML is inspected
    Then the agents section contains prompt_template for each agent
    And the agents section contains input_schema and output_schema as abstract names
    And the edges section contains source, target, edge_type and hitl_gate_config
    And the requires section lists connector_types and abstract_schemas

  Scenario: Export fails for non-existent pipeline
    When the user requests export of pipeline "00000000-0000-0000-0000-000000099999"
    Then the response status is 404
