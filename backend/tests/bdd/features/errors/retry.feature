Feature: Run Retry
  As a pipeline operator
  I want to retry a failed run from a specific node
  So that transient errors can be recovered without restarting

  Background:
    Given I am authenticated as an admin in org "acme"

  # @awaiting-implementation: the /retry endpoint does not exist in the current API
  # (retry/recovery is per-node via POST /runs/{id}/nodes/{node_id}/recover).
  @awaiting-implementation
  Scenario: Retry from failed node
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/retry with from_node "node-2"
    Then a new run is created
    And the new run starts from node 2
    And node 1 is not re-executed

  # @awaiting-implementation: the /retry endpoint does not exist in the current API
  # (retry/recovery is per-node via POST /runs/{id}/nodes/{node_id}/recover).
  @awaiting-implementation
  Scenario: Retry resets downstream state
    Given a run that failed at node 2 of 3
    And node 3 had completed successfully before failure
    When I POST /api/runs/{run_id}/retry with from_node "node-2"
    Then node 3 state is reset
    And nodes 2 and 3 are re-executed

  # @awaiting-implementation: the /retry endpoint does not exist in the current API
  # (retry/recovery is per-node via POST /runs/{id}/nodes/{node_id}/recover).
  @awaiting-implementation
  Scenario: Retry from start restarts the entire pipeline
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/retry with from_node "node-1"
    Then a new run is created
    And all 3 nodes are re-executed

  # @awaiting-implementation: the /retry endpoint does not exist in the current API
  # (retry/recovery is per-node via POST /runs/{id}/nodes/{node_id}/recover).
  @awaiting-implementation
  Scenario: Retry with new run_context
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/retry with from_node "node-2" and run_context {"fix": "true"}
    Then the new run has run_context with fix "true"

  # @awaiting-implementation: the /retry endpoint does not exist in the current API
  # (retry/recovery is per-node via POST /runs/{id}/nodes/{node_id}/recover).
  @awaiting-implementation
  Scenario: Retry on successful run is rejected
    Given a completed run
    When I POST /api/runs/{run_id}/retry with from_node "node-1"
    Then the response status is 409
    And the error mentions "already completed"
