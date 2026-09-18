Feature: Feedback Handler
  As a pipeline author
  I want rejected HITL gates to create structured feedback records
  So that I can review, triage, and learn from human rejections

  Scenario: Create feedback record on rejection
    Given a run is waiting at gate "review-output"
    And I am authenticated as an approver
    When I POST feedback for run with rejection reason "Output lacks required citations"
    Then the response status is 201
    And the feedback record status is "pending"

  Scenario: List feedback records
    Given a feedback record exists for the current run
    When I GET /api/v1/feedback
    Then the response contains at least one feedback item

  Scenario: Update feedback status
    Given a feedback record exists with status "pending"
    When I PATCH the feedback record status to "routing"
    Then the feedback record status becomes "routing"

  Scenario: Invalid feedback status transition is rejected
    Given a feedback record exists with status "resolved"
    When I PATCH the feedback record status to "pending"
    Then the response status is 422

  Scenario: Create correction run from feedback review
    Given a feedback record exists with handler type "ai_correction"
    When I review the feedback record with action "create_correction_run"
    Then a correction run is spawned
    And the feedback status becomes "correcting"
