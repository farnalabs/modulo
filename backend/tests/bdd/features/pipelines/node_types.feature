Feature: Pipeline Node Types
  As a pipeline author
  I want to define different node types in my pipeline
  So that I can model diverse workflow steps

  Scenario: Standard agent node executes normally
    Given a pipeline with a standard agent node "analyze"
    When the run reaches node "analyze"
    Then the node executes successfully
    And an artifact is recorded

  Scenario: Manual node pauses for human input
    Given a pipeline with a manual node "review-data"
    When the run reaches node "review-data"
    Then the run pauses for human input
    And the run status becomes "awaiting_human"

  Scenario: Manual node resumes with human output
    Given a pipeline with a manual node "review-data"
    And the run is waiting at node "review-data"
    When human output is provided
    Then the run continues
    And the manual output is available in artifacts

  Scenario: HITL gate node interrupts for approval
    Given a pipeline with a HITL gate node "pre-deploy"
    When the run reaches the "pre-deploy" gate
    Then the run status becomes "waiting_for_approval"
