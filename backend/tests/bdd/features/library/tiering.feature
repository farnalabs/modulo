Feature: Integration tier classification for library primitives
  Library primitives are classified into a tier — native, preview, or
  in_dev — so the frontend can visually distinguish integration stability
  (ADR 010). Creating a primitive with an explicit tier persists it;
  omitting the tier defaults to native.

  Background:
    Given the organisation exists

  Scenario: Create a library primitive with an explicit preview tier
    When the user creates a library primitive with body
      """
      {
        "primitive_type": "schema",
        "name": "Draft Schema",
        "slug": "draft-schema",
        "content_json": {},
        "tier": "preview"
      }
      """
    Then the response status is 201
    And the response has tier "preview"

  Scenario: Create a library primitive without a tier defaults to native
    When the user creates a library primitive with body
      """
      {
        "primitive_type": "schema",
        "name": "Standard Schema",
        "slug": "standard-schema",
        "content_json": {}
      }
      """
    Then the response status is 201
    And the response has tier "native"
