Feature: Audit Event Recording
  As a security administrator
  I want all significant actions recorded in an immutable audit trail
  So that I can review who did what and when

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Pipeline creation is audited
    When I POST /api/pipelines with name "my-pipeline" and valid config
    Then an audit event is created with type "pipeline.created"
    And the audit event records the actor "admin"
    And the audit event records the resource "my-pipeline"

  Scenario: Pipeline deletion is audited
    Given org "acme" has pipeline "obsolete"
    When I DELETE /api/pipelines/obsolete
    Then an audit event is created with type "pipeline.deleted"
    And the audit event records the pipeline id

  Scenario: Run trigger is audited
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then an audit event is created with type "run.created"

  Scenario: HITL decision is audited
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I approve the run
    Then an audit event is created with type "hitl.approved"

  Scenario: Audit log is paginated
    Given 25 audit events exist
    When I GET /api/admin/audit?limit=10
    Then the response contains 10 audit events
    And the response has a next cursor

  Scenario: Audit events are immutable
    Given an audit event exists
    When I attempt to modify the audit event
    Then the modification is rejected

  Scenario: Audit events have cryptographic chaining
    Given 3 audit events exist
    When I verify the audit chain
    Then each event has a previous_hash linking to the prior event
    And the chain is valid
