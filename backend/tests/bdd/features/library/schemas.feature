Feature: Library Schemas
  Users can create, list, and manage schema definitions in the library.
  Each schema is a named JSON Schema definition that can be used as
  a document type for library primitives.

  Background:
    Given the organisation exists

  Scenario: Create a new schema
    When the user sends POST /api/v1/schemas with body
      """
      {"name": "meeting-notes", "description": "Structured meeting notes"}
      """
    Then the response status is 201
    And the response contains a schema with name "meeting-notes"
    And the response contains a schema id

  Scenario: Create a schema version
    Given a schema "meeting-notes" exists
    When the user sends POST /api/v1/schemas/{schema_id}/versions with body
      """
      {
        "version": "1.0",
        "version_number": 1,
        "definition_json": {
          "type": "object",
          "title": "Meeting Notes",
          "properties": {
            "title": {"type": "string", "description": "Meeting title"},
            "date": {"type": "string", "format": "date", "description": "Meeting date"}
          },
          "required": ["title", "date"]
        },
        "published": true
      }
      """
    Then the response status is 201
    And the response contains a schema version with version "1.0"

  Scenario: List all schemas
    Given 22 library schemas exist
    When the user requests GET /api/v1/schemas
    Then the response status is 200
    And the response contains at least 22 schemas

  Scenario: List schemas with pagination
    Given 22 library schemas exist
    When the user requests GET /api/v1/schemas?page=1&page_size=10
    Then the response status is 200
    And the response contains 10 schemas

  Scenario: Get a single schema
    Given a schema "meeting-notes" exists
    When the user requests GET /api/v1/schemas/{schema_id}
    Then the response status is 200
    And the response contains a schema with name "meeting-notes"

  Scenario: Get non-existent schema returns 404
    When the user requests GET /api/v1/schemas/00000000-0000-0000-0000-000000099999
    Then the response status is 404

  Scenario: Update a schema
    Given a schema "meeting-notes" exists
    When the user sends PATCH /api/v1/schemas/{schema_id} with body
      """
      {"description": "Updated meeting notes schema"}
      """
    Then the response status is 200
    And the response contains a schema with description "Updated meeting notes schema"

  Scenario: Deprecate a schema
    Given a schema "meeting-notes" exists
    When the user sends PATCH /api/v1/schemas/{schema_id}/deprecate
    Then the response status is 200
    And the response contains a schema that is deprecated

  Scenario: Delete a schema
    Given a schema "meeting-notes" exists
    And no agents reference the schema
    When the user sends DELETE /api/v1/schemas/{schema_id}
    Then the response status is 204

  Scenario: List schema versions
    Given a schema "meeting-notes" exists
    And the schema has 2 versions
    When the user requests GET /api/v1/schemas/{schema_id}/versions
    Then the response status is 200
    And the response contains 2 schema versions

  Scenario: Get a specific schema version
    Given a schema "meeting-notes" exists
    And the schema has a version "1.0"
    When the user requests GET /api/v1/schemas/{schema_id}/versions/1.0
    Then the response status is 200
    And the response version is "1.0"

  Scenario: Validate a valid JSON Schema
    When the user sends POST /api/v1/schemas/validate with body
      """
      {
        "definition": {
          "type": "object",
          "properties": {
            "name": {"type": "string"}
          }
        }
      }
      """
    Then the response status is 200
    And the validation result is valid

  Scenario: Validate an invalid JSON Schema
    When the user sends POST /api/v1/schemas/validate with body
      """
      {
        "definition": {
          "type": 123
        }
      }
      """
    Then the response status is 200
    And the validation result is not valid

  Scenario: Import a JSON Schema
    When the user sends POST /api/v1/schemas/import with body
      """
      {
        "content": "{\"type\": \"object\", \"title\": \"Test\", \"properties\": {\"name\": {\"type\": \"string\"}}, \"required\": [\"name\"]}"
      }
      """
    Then the response status is 200
    And the imported schema has name "Test"
    And the imported schema has 1 field
