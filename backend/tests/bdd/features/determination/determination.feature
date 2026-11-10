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

  Scenario: Operator reviews and approves a determination proposal
    Given I am authenticated as an operator
    When I submit an approve decision acknowledging unresolved assumptions
    Then the response status is 200
    And the proposal decision is approve
    And the proposal review state is approved
    And the proposal activation status is not_activated

  Scenario: Operator reject review decision requires a reason
    Given I am authenticated as an operator
    When I submit a reject decision with no reason
    Then the response status is 422

  Scenario: Operator approve decision is blocked by unacknowledged assumptions
    Given I am authenticated as an operator
    When I submit an approve decision without acknowledging low confidence assumptions
    Then the response status is 422

  Scenario: Operator compares two determination proposals
    Given I am authenticated as an operator
    When I request POST /api/v1/determination/compare
    Then the response status is 200
    And the comparison contract version is alpha.determination-comparison.v1
    And the comparison activation status is not_activated

  Scenario: Operator reruns determination against a prior proposal
    Given I am authenticated as an operator
    And connected tools are configured
    When I request POST /api/v1/determination/rerun
    Then the response status is 200
    And the rerun contract version is alpha.determination-rerun.v1
