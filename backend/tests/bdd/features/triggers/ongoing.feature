Feature: Ongoing Trigger
  As a pipeline operator
  I want a trigger that keeps a pipeline topped up to a target number of in-flight runs
  So that my pipeline always has capacity available

  Scenario: Ongoing trigger tops up below target
    Given a pipeline with max_concurrent_runs 3
    And an ongoing trigger with target 2 and scan interval 60 seconds
    And the pipeline has 0 in-flight runs
    When the ongoing scheduler top-up runs
    Then exactly 2 runs are created
    And each created run references the ongoing trigger

  Scenario: Ongoing trigger at target does not top up
    Given a pipeline with max_concurrent_runs 3
    And an ongoing trigger with target 2 and scan interval 60 seconds
    And the pipeline has 2 in-flight runs
    When the ongoing scheduler top-up runs
    Then no runs are created

  Scenario: Ongoing trigger above target does not top up
    Given a pipeline with max_concurrent_runs 3
    And an ongoing trigger with target 2 and scan interval 60 seconds
    And the pipeline has 3 in-flight runs
    When the ongoing scheduler top-up runs
    Then no runs are created

  Scenario: Ongoing trigger respects the daily spend limit
    Given a pipeline with max_concurrent_runs 3
    And an ongoing trigger with target 2 and scan interval 60 seconds
    And the ongoing trigger has a daily spend limit of 50.00
    And the ongoing trigger's org has accumulated 60.00 in run costs today
    And the pipeline has 0 in-flight runs
    When the ongoing scheduler top-up runs
    Then no runs are created

  Scenario: Ongoing trigger pauses when the org is paused
    Given a pipeline with max_concurrent_runs 3
    And an ongoing trigger with target 2 and scan interval 60 seconds
    And the org is paused
    And the pipeline has 0 in-flight runs
    When the ongoing scheduler top-up runs
    Then no runs are created

  Scenario: Pending runs count toward the ongoing target
    Given a pipeline with max_concurrent_runs 3
    And an ongoing trigger with target 2 and scan interval 60 seconds
    And the pipeline has 2 pending runs
    When the ongoing scheduler top-up runs
    Then no runs are created
