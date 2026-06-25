Feature: Browse Library Primitives
  As a pipeline author
  I want to browse, search and filter library primitives
  So that I can discover reusable agents, schemas and connectors

  Background:
    Given the organisation has 3 local primitives
    And 5 community primitives exist

  Scenario: List all primitives
    When the user requests GET /api/v1/libraries
    Then the response contains 8 primitives total
    And each primitive has id, name, primitive_type, source, and version

  Scenario: Filter by primitive type
    When the user requests GET /api/v1/libraries?primitive_type=schema
    Then the response contains only schema-type primitives
    And at least 2 schemas are returned

  Scenario: Search by name or description
    When the user requests GET /api/v1/libraries?search=PRD
    Then the response contains primitives whose name or description matches "PRD"

  Scenario: Filter local only
    When the user requests GET /api/v1/libraries?source=local
    Then the response contains only organisation-local primitives
    And no community primitives are included

  Scenario: View single primitive detail
    Given a specific primitive exists with id "00000000-0000-0000-0000-000000000010"
    When the user requests GET /api/v1/libraries/00000000-0000-0000-0000-000000000010
    Then the response has name "PRD Input Schema"
    And the response has primitive_type "schema"
