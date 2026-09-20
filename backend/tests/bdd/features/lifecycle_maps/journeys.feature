Feature: Lifecycle Map Journey Detail — tracking, history and self-report
  As a workflow operator
  I want to inspect a work item's journey through a lifecycle map and let
  external workflows self-report their progression
  So that I can track where each work item sits in the lifecycle

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Journey detail returns the current stage and run history
    Given lifecycle map "SDLC Workflow" has journey "issue" "FAR-100" at stage "Build"
    And the journey has run history with provenance "cron" and "manual"
    When I get the journey detail for "issue" "FAR-100"
    Then the response status is 200
    And the journey detail has kind "issue" and ref "FAR-100"
    And the journey detail reports current stage "Build"
    And the journey detail reports 2 runs with provenance "cron" and "manual"

  Scenario: Journey detail for an unknown journey returns 404
    Given lifecycle map "SDLC Workflow" has no journey "issue" "FAR-999"
    When I get the journey detail for "issue" "FAR-999"
    Then the response status is 404

  Scenario: The map journey list is keyset-paginated and honours the ref filter
    Given lifecycle map "SDLC Workflow" exists
    When I list the map journeys filtered by ref "#123"
    Then the response status is 200
    And the journey list contains 2 journeys
    And the journey list carries a non-null next_cursor
    And the journey list was filtered by ref "#123"

  Scenario: Self-report advances only matched journeys
    Given lifecycle map "SDLC Workflow" has journey "issue" "FAR-100" at stage "Build"
    When I self-report work item "issue" "FAR-100" as complete
    Then the response status is 200
    And the self-report counts "1" accepted and "0" unmatched and "0" rejected
    And the matched journey was advanced with status "complete"

  Scenario: Self-report drops unmatched refs and rejects malformed entries
    Given lifecycle map "SDLC Workflow" exists
    When I self-report the work item refs "issue" "UNKNOWN-1" and a malformed entry
    Then the response status is 200
    And the self-report counts "0" accepted and "1" unmatched and "1" rejected