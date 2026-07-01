Feature: Composite Template CRUD
  As a pipeline author
  I want to create, read, update and delete composite templates
  So that I can build reusable sub-pipeline components with parameter ports

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a composite template with parameter ports
    When I POST /api/composite-templates with name "Devil's Advocate" and a sub-pipeline containing agent "critic"
    Then the response status is 201
    And the response contains a composite template id
    And the response has name "Devil's Advocate"
    And the response has version "0.1.0"

  Scenario: List all composite templates
    Given org "acme" has composite templates "template-a", "template-b"
    When I GET /api/composite-templates
    Then the response status is 200
    And the response contains 2 composite templates

  Scenario: Get composite template by id
    Given org "acme" has composite template "template-a" with id "00000000-0000-0000-0000-0000000000a1"
    When I GET /api/composite-templates/00000000-0000-0000-0000-0000000000a1
    Then the response status is 200
    And the response name is "template-a"

  Scenario: Update composite template name and description
    Given org "acme" has composite template "template-a" with id "00000000-0000-0000-0000-0000000000a1"
    When I PATCH /api/composite-templates/00000000-0000-0000-0000-0000000000a1 with new name "updated-name"
    Then the response status is 200

  Scenario: Delete composite template
    Given org "acme" has composite template "obsolete" with id "00000000-0000-0000-0000-0000000000a2"
    When I DELETE /api/composite-templates/00000000-0000-0000-0000-0000000000a2
    Then the response status is 204
    And the composite template no longer exists

  Scenario: Org-scoped isolation — cannot see another org's composites
    Given org "acme" has composite template "acme-only" with id "00000000-0000-0000-0000-0000000000a3"
    When the user from org "othercorp" requests GET /api/composite-templates/00000000-0000-0000-0000-0000000000a3
    Then the response status is 404

  Scenario: Creating a composite template with missing name returns 422
    When I POST /api/composite-templates with an empty name
    Then the response status is 422

  Scenario: Creating a composite template with invalid parameter port type returns 422
    When I POST /api/composite-templates with invalid port type "blob"
    Then the response status is 422
