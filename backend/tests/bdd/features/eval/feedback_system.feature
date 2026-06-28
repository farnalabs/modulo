Feature: Feedback System
  As a pipeline author
  I want to capture human feedback on pipeline output
  So that I can detect eval gaps and trigger correction runs

  Scenario: Create a human feedback record
    Given a pipeline run produced output
    When a human provides feedback on the output
    Then a FeedbackRecord is created with type human
    And the feedback status is "pending"

  Scenario: Feedback record transitions through valid states
    Given a feedback record with status "pending"
    When the status is changed to "routing"
    Then the feedback status is "routing"
    And the transition is allowed

  Scenario: Invalid status transition is rejected
    Given a feedback record with status "resolved"
    When the status is changed to "pending"
    Then the transition is rejected

  Scenario: Eval gap is detected when no eval catches a failure
    Given a pipeline run produced output
    And an eval suite that would pass the output
    When the system detects an eval gap
    Then the feedback record has eval_gap true

  Scenario: Correction run is spawned from feedback
    Given a feedback record with status "pending"
    And the feedback has a valid run_id
    When a correction run is spawned
    Then a new correction run is created
    And the feedback status becomes "correcting"
