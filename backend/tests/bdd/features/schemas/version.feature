Feature: Schema Versioning
  As a pipeline author
  I want to version my schemas
  So that changes are tracked and pipelines pin a specific version

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Schema update creates new version
    Given org "acme" has schema "review-input" version 1
    When I update the schema with new fields
    Then the schema version becomes 2

  Scenario: Pipeline snapshot pins schema version
    Given org "acme" has schema "review-input" version 1
    And a pipeline is published using schema version 1
    When I update the schema to version 2
    And I trigger a run using the pinned snapshot
    Then the run uses schema version 1

  Scenario: List schema versions
    Given org "acme" has schema "review-input"
    And the schema has been updated twice
    When I GET /api/schemas/review-input/versions
    Then the response contains 3 versions

  Scenario: View specific schema version
    Given org "acme" has schema "review-input" with 2 versions
    When I GET /api/schemas/review-input/versions/1
    Then the response has the original schema definition
