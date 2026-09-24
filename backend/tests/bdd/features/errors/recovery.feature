Feature: Run Recovery from a Failed State
  As a pipeline operator
  I want to recover a failed run by replaying or skipping a failed node
  So that I can fix issues and continue without data loss

  Node recovery is per-node via `POST /api/v1/runs/{run_id}/nodes/{node_id}/recover` —
  the run-level `/resume` / `/retry` endpoints these scenarios previously targeted do
  not exist. Replay (with `input_data`) re-runs the node with new manual output;
  skip (input_data null) marks it completed with no output. The scenarios drive the
  REAL route through the authenticated TestClient with only the `recover_node` DB
  seam and the `dispatch_run` resume seam patched.

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Replay a failed manual node resumes the run
    Given a failed run has a manual node "node-2" pending recovery
    When I replay node "node-2" with input_data {"answer": 42}
    Then the recovery request returns status 200
    And the recovery response action is "replay"
    And the recovery resume is dispatched with the recovered output

  Scenario: Skipping a failed manual node marks it complete and resumes the run
    Given a failed run has a manual node "node-2" pending recovery
    When I skip node "node-2"
    Then the recovery request returns status 200
    And the recovery response action is "skip"

  Scenario: Recovery refuses HITL gate nodes
    Given a failed run is parked at a HITL gate node "hitl_gate_approval"
    When I replay node "hitl_gate_approval" with input_data {"approve": true}
    Then the recovery request returns status 422
    And the recovery failure mentions "HITL"

  Scenario: Recovery of a node missing from the graph is 404
    Given a failed run has a manual node "node-2" pending recovery
    And the node "ghost-node" is not present in the graph
    When I recover node "ghost-node"
    Then the recovery request returns status 404

  Scenario: Recovery of an already-completed node is 409
    Given a failed run has a manual node "node-2" pending recovery
    And the node "node-2" has already completed
    When I recover node "node-2"
    Then the recovery request returns status 409
    And the recovery failure mentions "already completed"

  Scenario: Recovery is denied while the run is not in a recoverable state
    Given a failed run has a manual node "node-2" pending recovery
    And the run is in a state that does not permit recovery
    When I recover node "node-2"
    Then the recovery request returns status 409

  Scenario: Concurrent recovery attempts conflict
    Given a failed run has a manual node "node-2" pending recovery
    And another operator concurrently recovers the node
    When I recover node "node-2"
    Then the recovery request returns status 409

  Scenario: A failed resume enqueue surfaces a 500
    Given a failed run has a manual node "node-2" pending recovery
    And the resume enqueue fails after recovery
    When I recover node "node-2"
    Then the recovery request returns status 500
    And the recovery failure mentions "enqueue"

  Scenario: Viewers can inspect runs but cannot recover nodes
    Given a failed run has a manual node "node-2" pending recovery
    And I am authenticated as a viewer in org "acme"
    When I recover node "node-2"
    Then the recovery request returns status 403