Feature: Manual Node
  As a pipeline author
  I want a manual-input node that pauses the run for human data entry
  So that humans can provide structured input at defined points in the workflow

  Scenario: Manual node pauses for human input
    Given a manual input node exists in the pipeline
    When the run reaches the manual node
    Then the run pauses and waits for manual data submission
    And the run status becomes "awaiting_human"

  Scenario: Submit manual output resumes the run
    Given a run is waiting at manual node "review-data"
    And I submit manual output with valid data
    When the manual output is processed
    Then the run status becomes "running"
    And the run continues past the manual node

  Scenario: Manual output validated against schema
    Given a run is waiting at manual node "review-data"
    And the manual node has an output schema with required field "approval"
    When I submit manual output missing required field "approval"
    Then the response status is 422

  Scenario: Non-approver cannot submit manual output
    Given a run is waiting at manual node "review-data"
    And I am authenticated as a viewer (not an approver)
    When I submit manual output with valid data
    Then the response status is 403
