Feature: Eval Dashboard
  As a quality engineer
  I want to view eval results and trends for my pipelines
  So that I can track performance regressions over time

  Scenario: View eval results for a completed run
    Given I am on the eval dashboard page for a completed run
    When I view the eval results
    Then I see a list of eval results with pass/fail status

  Scenario: Filter eval runs by pass/fail status
    Given there are eval runs with both pass and fail statuses
    When I filter by failed runs
    Then only failed runs are shown in the list

  Scenario: Compare two runs side-by-side
    Given I am on the eval dashboard page for a completed run
    When I select a second run to compare
    Then I see a side-by-side comparison of eval results

  Scenario: Empty state when no evals exist
    Given I am on the eval dashboard page with no eval runs
    Then I see an empty state message
