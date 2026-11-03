Feature: System Maps revision lifecycle
  As an internal operator
  I want to publish, compare, restore and time-travel System Maps revisions
  So that a published map is a validated, attributable snapshot of the system

  Publishing validates the graph and returns 200 with validation errors when it
  fails (the revision is NOT published). Diffs compare two revisions. Restoring
  an archived/published revision always creates a NEW draft. The version-aware
  read returns the revision effective at a timestamp.

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Publishing an invalid graph is rejected with validation errors
    Given a draft revision whose graph fails publish validation
    When I publish the revision
    Then the response status is 200
    And the publish response carries validation errors
    And the invalid revision was not published

  Scenario: Diffing two revisions reports the added node
    Given a published revision and a newer draft with one added node
    When I diff the published revision against the draft
    Then the response status is 200
    And the diff report names the added node

  Scenario: Restoring an archived revision creates a new draft
    Given an archived revision with revision number 2
    When I restore the revision as new
    Then the response status is 200
    And the restore response names a new draft revision
    And the restore response cites the source revision

  Scenario: Restoring a draft revision is rejected
    Given a draft revision with revision number 1
    When I restore the revision as new
    Then the response status is 409

  Scenario: Reading the revision active at a timestamp
    Given a published revision effective from "2026-10-01T00:00:00+00:00"
    When I read the revision active at "2026-10-02T12:00:00+00:00"
    Then the response status is 200
    And the by-time read reports the published revision

  Scenario: No revision is active before the first publish
    Given no revision is effective at "2026-09-01T00:00:00+00:00"
    When I read the revision active at "2026-09-01T00:00:00+00:00"
    Then the response status is 404
    And the by-time read reports no active revision
