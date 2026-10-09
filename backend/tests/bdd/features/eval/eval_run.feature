Feature: Eval Run
  As a pipeline author
  I want to run evaluation suites against my pipeline
  So that I can measure quality and catch regressions

  Scenario: Trigger an eval run
    Given pipeline "my-pipeline" has eval suite "basic-suite"
    When the eval suite trigger fires
    Then the eval run starts with status "pending"

  Scenario: Eval run scores cases
    Given an eval run with 3 test cases
    When the eval engine processes all cases
    Then each case has a score
    And the eval run has an aggregate score

  Scenario: Eval run below threshold does not pass
    Given an eval suite with pass_threshold 0.8
    And an eval run whose case scored 0.65
    When the eval run completes
    Then the eval run status is "completed"
    And no case passed the eval

  Scenario: Eval results are visible in the UI
    Given a completed eval run with scores
    When I navigate to the eval results page
    Then I see per-case scores and the aggregate
