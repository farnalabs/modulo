Feature: Schema Migration Dry-Run
  As a pipeline author
  I want to dry-run a schema migration before applying it
  So that I can inspect the changes before committing to them

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Dry-run migration returns the plan without modifying data
    Given a source schema version with fields "full_name" (string) and "count" (integer)
    And a target schema version with fields "display_name" (string), "count" (integer) and "email" (string)
    When I POST /api/v1/schemas/migrate with dry_run=true
    Then the response status is 200
    And the response includes a migration plan
    And the response includes dry_run: true
    And the migrated_data equals the original input

  Scenario: Dry-run does not alter original data
    Given a source schema version with a field "full_name" (string)
    And a target schema version with a field "display_name" (string)
    When I POST /api/v1/schemas/migrate with dry_run=true and data {"full_name": "Alice"}
    Then the response status is 200
    And the migrated_data still contains "full_name"

  Scenario: Migration plan endpoint previews changes
    Given a source definition with field "full_name" (string)
    And a target definition with field "display_name" (string)
    When I POST /api/v1/schemas/migrate/plan
    Then the response status is 200
    And the plan contains a rename from "full_name" to "display_name"

  Scenario: Dry-run detects field additions
    Given a source schema version with fields "name" (string)
    And a target schema version with fields "name" (string) and "email" (string)
    When I POST /api/v1/schemas/migrate with dry_run=true
    Then the response status is 200
    And the plan lists "email" in field_additions
