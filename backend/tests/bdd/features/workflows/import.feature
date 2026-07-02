Feature: Import workflow from bundle
  Users can import a .modulo.zip bundle to recreate a pipeline.
  The import pipeline parses with yaml.safe_load, verifies Ed25519
  signatures, resolves connector/schema bindings, and handles name
  and schema conflicts with disambiguation suffixes.

  Background:
    Given the organisation has a "filesystem" connector instance
    And has a model backend "claude-sonnet-4"
    And has a schema "PRD Input Schema" with abstract_name "prd-input"

  Scenario: Import valid pipeline bundle
    When the user sends POST /api/v1/libraries/import/confirm with bundle_json
    Then the response status is 200
    And a new pipeline is created with the bundle's name
    And connector bindings are resolved to local instances
    And schema references are resolved to local schemas by abstract_name

  Scenario: Import rejects tampered bundle with invalid Ed25519 signature
    Given a bundle with a mismatched Ed25519 signature
    When the user sends POST /api/v1/libraries/import/confirm with bundle_json
    Then the response status is 422
    And the error message mentions the tampered bundle
    And no pipeline entity is created

  Scenario: Import resolves connector type conflicts with disambiguation
    Given the bundle references connector type "filesystem"
    And the organisation has 2 "filesystem" connector instances
    When the import analysis resolves connectors
    Then a connector conflict is detected
    And the available connector instances are listed for user selection

  Scenario: Import resolves schema version conflicts with disambiguation suffix
    Given the bundle embeds a schema with abstract_name "prd-input"
    And the local schema has a different field structure than the bundle
    When the import is confirmed
    Then a warning is emitted for the schema conflict
    And the imported schema is saved with a disambiguation suffix
    And the local schema is unchanged

  Scenario: Import handles duplicate pipeline names with suffix
    Given a pipeline named "PRD to Tickets" already exists
    When the user imports a bundle containing pipeline "PRD to Tickets"
    Then the response contains name_conflicts
    And the suggested name is "PRD to Tickets (imported)"
