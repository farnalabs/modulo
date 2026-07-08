Feature: Schema Deletion Protection
  As a pipeline operator
  I want schemas protected from deletion when they are in use
  So that I don't accidentally break running pipelines

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Unused schema can be deleted
    Given org "acme" has schema "unused-schema"
    And no pipeline uses "unused-schema"
    When I DELETE /api/schemas/unused-schema
    Then the response status is 204
    And the schema no longer exists

  Scenario: Schema used by pipeline cannot be deleted
    Given org "acme" has schema "in-use-schema"
    And a pipeline uses "in-use-schema"
    When I DELETE /api/schemas/in-use-schema
    Then the response status is 409
    And the error mentions "in use by pipeline"

  Scenario: Force delete bypasses protection
    Given org "acme" has schema "in-use-schema"
    And a pipeline uses "in-use-schema"
    When I DELETE /api/schemas/in-use-schema with force=true
    Then the response status is 204

  Scenario: Schema used only by unpinned pipeline can be deleted
    Given org "acme" has schema "legacy-schema"
    And an unpublished pipeline uses "legacy-schema"
    When I DELETE /api/schemas/legacy-schema
    Then the response status is 409
