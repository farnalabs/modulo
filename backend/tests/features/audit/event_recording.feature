Feature: Audit Event Recording
  As a security administrator
  I want all significant actions recorded in an immutable audit trail
  So that I can review who did what and when

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Append an audit event with actual event type
    When I append an audit event of type "pipeline.autonomy_level_changed" for pipeline "pipeline-abc"
    Then the event has a valid SHA-256 hash
    And the event records the actor "admin"

  Scenario: Append multiple events form a verifiable chain
    Given the audit chain is empty
    When I append an audit event of type "fernet_key_rotation_started" for key version "v2"
    And I append an audit event of type "fernet_key_rotation_completed" for key version "v2"
    Then the chain has 2 events
    And each event has a previous_hash linking to the prior event
    And the chain is valid

  Scenario: HITL output delivery is audited
    Given a run has output delivered to gate "pre-deploy"
    When I deliver output for gate "pre-deploy"
    Then an audit event is created with type "hitl.output_delivered"
    And the event records the run ID
    And the event records the output hash

  Scenario: Claim expiry is audited
    Given a HITL gate claim has expired
    Then an audit event is created with type "hitl.claim_expired"

  Scenario: Org deletion request is audited
    When an org deletion is requested
    Then an audit event is created with type "org_deletion_requested"

  Scenario: Audit log is paginated
    Given 25 audit events exist
    When I GET /api/v1/admin/audit?limit=10
    Then the response contains 10 audit events
    And the response has a next_cursor field

  Scenario: Audit events are immutable
    Given an audit event exists
    When I attempt to modify the audit event
    Then the modification is rejected

  Scenario: Audit events have cryptographic chaining
    Given 3 audit events exist
    When I verify the audit chain
    Then each event has a previous_hash linking to the prior event
    And the chain is valid
