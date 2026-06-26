Feature: Import workflow from bundle
  Users can import a .modulo.zip bundle to recreate a pipeline in their
  organisation. The import wizard resolves schemas, connectors, and model
  backends to local equivalents.

  Background:
    Given the organisation has schemas "PRD Input Schema" and "Requirements Output Schema"
    And has an active "filesystem" connector instance
    And has an active model backend "claude-sonnet-4"

  Scenario: Upload ZIP and analyse (server-side)
    When the user uploads a valid .modulo.zip to POST /api/v1/libraries/import/upload-zip
    Then the response status is 200
    And the response contains resolved_schemas with at least 2 entries
    And the response contains available_teams
    And the response contains bundle_json with the serialized bundle

  Scenario: Analyse a bundle JSON directly
    When the user sends POST /api/v1/libraries/import/analyse with a bundle
    Then the response contains warnings if any references are unresolvable
    And the response contains name_conflicts if pipeline names collide

  Scenario: Import with name conflict resolves via suffix
    Given a pipeline named "My Pipeline" already exists
    When the user imports a bundle containing "My Pipeline"
    Then the name_conflicts list includes a pipeline conflict
    And the suggested name is "My Pipeline (imported)"

  Scenario: Import materializes real entities
    When the user sends POST /api/v1/libraries/import/confirm with bundle_json
    Then the response contains pipeline_id pointing to a new Pipeline
    And agent_count matches the number of agents in the bundle
    And a library primitive is created for the workflow

  Scenario: Import with schema resolution
    Given the bundle references "prd-input" schema by abstract_name
    When the import analysis runs
    Then the schema is resolved to the existing local "PRD Input Schema"
    And no schema creation warning is emitted

  Scenario: Import with unresolved schema creates new schema
    Given the bundle references an unknown schema
    When the import is confirmed
    Then a new Schema and SchemaVersion are created for the unknown schema

  Scenario: Import assigns ownership
    When the user imports a bundle with owner_team_id set
    Then the created pipeline has owner_team_id matching the selection
    And the library primitive has owner_team_id matching the selection

  Scenario: Import non-ZIP file rejected
    When the user uploads a .txt file to POST /api/v1/libraries/import/upload-zip
    Then the response status is 400
