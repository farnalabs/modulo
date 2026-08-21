Feature: Schema Migration
  As a pipeline author
  I want to dry-run and apply a schema migration
  So that I can inspect and execute changes between schema versions safely

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Dry-run migration returns the plan without modifying data
    Given a source schema version with fields {"full_name": "string", "count": "integer"}
    And a target schema version with fields {"display_name": "string", "count": "integer", "email": "string"}
    When I POST /api/v1/schemas/migrate with dry_run=true
    Then the response status is 200
    And the response includes a migration plan
    And the response includes dry_run: true
    And the migrated_data equals the original input

  Scenario: Dry-run does not alter original data
    Given a source schema version with fields {"full_name": "string"}
    And a target schema version with fields {"display_name": "string"}
    When I POST /api/v1/schemas/migrate with dry_run=true and data {"full_name": "Alice"}
    Then the response status is 200
    And the migrated_data still contains "full_name"

  Scenario: Migration plan endpoint previews changes
    Given a source definition with field {"full_name": "string"}
    And a target definition with field {"display_name": "string"}
    When I POST /api/v1/schemas/migrate/plan
    Then the response status is 200
    And the plan contains a rename from "full_name" to "display_name"

  Scenario: Migration plan endpoint records an audit event
    Given a source definition with field {"full_name": "string"}
    And a target definition with field {"display_name": "string"}
    When I POST /api/v1/schemas/migrate/plan
    Then the response status is 200
    And an audit event "schema_migration_planned" is recorded

  Scenario: Migration plan endpoint requires authentication
    Given a source definition with field {"full_name": "string"}
    And a target definition with field {"display_name": "string"}
    When I POST /api/v1/schemas/migrate/plan unauthenticated
    Then the response status is 401

  Scenario: Migration plan pairs same-type renames deterministically
    Given a source definition with fields {"a": "string", "b": "string"}
    And a target definition with fields {"x": "string", "y": "string"}
    When I POST /api/v1/schemas/migrate/plan
    Then the response status is 200
    And the plan contains a rename from "a" to "x"
    And the plan contains a rename from "b" to "y"

  Scenario: Dry-run detects field additions
    Given a source schema version with fields {"name": "string"}
    And a target schema version with fields {"name": "string", "email": "string"}
    When I POST /api/v1/schemas/migrate with dry_run=true
    Then the response status is 200
    And the plan lists "email" in field_additions

  Scenario: Applying a migration transforms the data and removes dropped fields
    Given a source schema version with fields {"name": "string", "legacy": "boolean"}
    And a target schema version with fields {"name": "string", "email": "string"}
    When I POST /api/v1/schemas/migrate with data {"name": "Alice", "legacy": true}
    Then the response status is 200
    And the migrated_data still contains "name"
    And the migrated_data no longer contains "legacy"
    And an audit event "schema_migration_completed" is recorded

  Scenario: Dry-run migration records an audit event
    Given a source schema version with fields {"name": "string"}
    And a target schema version with fields {"name": "string", "email": "string"}
    When I POST /api/v1/schemas/migrate with dry_run=true
    Then the response status is 200
    And an audit event "schema_migration_completed" is recorded with dry_run: true

  Scenario: Best-effort migration applies the reachable prefix of a partial chain
    Given a migration registry with a migration registered from "1.0.0" to "2.0.0"
    When I apply a partial migration from "1.0.0" to "3.0.0" on data {"name": "Alice"}
    Then the partial migration applied the steps "1.0.0->2.0.0"
    And the partial migration reports a chain gap

  Scenario: Best-effort migration applies a complete chain with no gaps
    Given a migration registry with a migration registered from "1.0.0" to "2.0.0"
    And a migration registered from "2.0.0" to "3.0.0"
    When I apply a partial migration from "1.0.0" to "3.0.0" on data {"name": "Alice"}
    Then the partial migration applied the steps "1.0.0->2.0.0,2.0.0->3.0.0"
    And the partial migration reports no chain gaps

  Scenario: Best-effort migration passes data through when no chain exists
    Given a migration registry with no registered migrations
    When I apply a partial migration from "1.0.0" to "2.0.0" on data {"name": "Alice"}
    Then the partial migration reports a chain gap
    And the migrated data still contains "name"

  Scenario: Best-effort migration dry-runs the reachable prefix without mutating data
    Given a migration registry with a migration registered from "1.0.0" to "2.0.0"
    And a migration registered from "2.0.0" to "3.0.0"
    When I dry-run a partial migration from "1.0.0" to "3.0.0" on data {"name": "Alice"}
    Then the partial migration reports no chain gaps
    And the dry-run describes "2" steps
    And the migrated data still contains "name"
