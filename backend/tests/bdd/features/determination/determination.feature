Feature: SDLC Assessment and Pipeline Draft Generation
  As a platform operator
  I want to scan connected tools for SDLC maturity assessment and generate editable pipeline drafts
  So that I can understand and automate the team's software delivery workflow

  Scenario: Operator scans connected tools for an SDLC assessment
    Given I am authenticated as an operator
    And connected tools are configured
    When I request GET /api/v1/determination
    Then the response status is 200
    And the response contains a determination summary
    And the response contains sample results

  Scenario: Operator generates a pipeline draft
    Given I am authenticated as an operator
    And connected tools are configured
    When I request POST /api/v1/determination/draft
    Then the response status is 200
    And the response contains draft nodes and edges

  Scenario: Viewer cannot access determination
    Given I am authenticated as a viewer
    When I request GET /api/v1/determination
    Then the response status is 403

  Scenario: No connected tools produces an empty assessment
    Given I am authenticated as an operator
    And no connected tools are configured
    When I request GET /api/v1/determination
    Then the response status is 200
    And the response has an empty sample list
