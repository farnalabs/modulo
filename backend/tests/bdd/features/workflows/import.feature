Feature: Import workflow from bundle
  Users can import a .modulo.zip bundle to recreate a pipeline.
  The import surface is two-phase: POST /api/v1/libraries/import/analyse
  resolves every bundle reference (connector types, abstract schemas, model
  backends) against the organisation, then POST /api/v1/libraries/import/confirm
  materialises the resolved entities. Both phases are exercised here through the
  real route handlers with only the DB read / materialisation seams patched —
  request validation, RLS scoping, resolution orchestration, name-conflict
  detection, error mapping and response serialisation all run for real.

  Background:
    Given the organisation has a "filesystem" connector instance
    And has a model backend "claude-sonnet-4"
    And has a schema "PRD Input Schema" with abstract_name "prd-input"

  Scenario: Import valid pipeline bundle
    When the user runs the import analysis on a valid bundle
    Then the connector binding is resolved to the local "filesystem" instance
    And the schema reference is resolved to the local "PRD Input Schema" by abstract_name
    And the model backend is resolved to the local "claude-sonnet-4" model backend
    When the user confirms the import with the analysed bundle
    Then the response status is 200
    And a new pipeline is created with the bundle's name

  Scenario: Import rejects a tampered bundle without creating a pipeline
    Given a bundle whose bundle_json is not valid JSON
    When the user attempts the import of the tampered bundle
    Then the response status is 400
    And the error message mentions the invalid bundle JSON
    And no pipeline entity is created

  Scenario: Import resolves connector type conflicts with disambiguation
    Given the bundle references connector type "filesystem"
    And the organisation has 2 "filesystem" connector instances
    When the import analysis resolves connectors
    Then the connector conflict is resolved to a single local instance
    And resolved_connectors contains a match with instance_id and instance_name

  Scenario: Import resolves schema references by abstract_name
    Given the bundle embeds a schema with abstract_name "prd-input"
    And the local schema has a different field structure than the bundle
    When the import analysis resolves schemas
    Then the schema is matched to the local schema by abstract_name
    And no schema creation warning is emitted

  Scenario: Import handles duplicate pipeline names with suffix
    Given a pipeline named "PRD to Tickets" already exists
    When the user runs the import analysis on a bundle named "PRD to Tickets"
    Then the response contains name_conflicts if pipeline names collide
    And the suggested name is "PRD to Tickets (imported)"
