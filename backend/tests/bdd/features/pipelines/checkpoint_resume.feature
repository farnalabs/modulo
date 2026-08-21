Feature: Checkpoint and Resume
  As a user
  I want failed runs to be resumable from the last checkpoint
  So that I don't have to restart long pipelines from scratch

  Scenario: Run state is checkpointed after each node
    Given a running pipeline with 3 nodes
    When node 1 completes
    Then a checkpoint exists for the run at node 1

  Scenario: Run resumes from checkpoint after failure
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/resume
    Then the run restarts from node 2
    And node 1 is not re-executed

  Scenario: Checkpoint uses AsyncPostgresSaver
    Given a running pipeline
    When state is persisted
    Then it is written to the PostgreSQL checkpoints table via asyncpg
