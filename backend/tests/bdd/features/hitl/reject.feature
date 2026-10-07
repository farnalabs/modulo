# FAR-1533: these scenarios drive a REAL run end to end. The steps live in
# tests/integration/test_hitl_reject_bdd.py (not tests/bdd/steps) because they
# need the real-Postgres fixtures from tests/integration/conftest.py: each
# scenario seeds a pipeline with a HITL gate, runs it through the real
# PipelineExecutor until it interrupts at the gate, commits a human rejection,
# resumes it through pipeline_execution.resume_run, and reads the run's
# terminal status and error_code back from the database.
#
# Runtime semantics (FAR-1487): a rejection with no reject route and no explicit
# `on_reject: proceed` ENDS the run `rejected` (`error_code=hitl.rejected`;
# `hitl.superseded` for a coalesced-supersede system rejection). A reject route
# still diverts the run; `on_reject: proceed` still continues it.
@far-1487 @far-1533
Feature: HITL Reject
  As an approver
  I want to reject a run waiting at a HITL review
  So that the run stops and is marked as rejected

  Scenario: Reject with no reject route ends the run as rejected
    Given a pipeline whose HITL gate has no reject route
    And a run is waiting at the gate
    When the approver rejects the gate with reason "Not ready"
    Then the run status is "rejected"
    And the run error code is "hitl.rejected"
    And the run did not execute node "b"

  Scenario: Reject with a reject route diverts the run
    Given a pipeline whose HITL gate has a reject route to node "fixer"
    And a run is waiting at the gate
    When the approver rejects the gate with reason "Needs a fix"
    Then the run status is "complete"
    And the run has no error code
    And the run executed node "fixer"
    And the run did not execute node "b"

  Scenario: on_reject proceed continues the run after a rejection
    Given a pipeline whose HITL gate has on_reject set to "proceed"
    And a run is waiting at the gate
    When the approver rejects the gate with reason "Not ready"
    Then the run status is "complete"
    And the run has no error code
    And the run executed node "b"

  Scenario: A coalesced supersede ends the run as rejected with hitl.superseded
    Given a pipeline whose HITL gate has no reject route
    And a run is waiting at the gate
    When the gate is superseded by a newer gate
    Then the run status is "rejected"
    And the run error code is "hitl.superseded"
    And the run did not execute node "b"
