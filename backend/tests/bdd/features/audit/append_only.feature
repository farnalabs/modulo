Feature: Audit Append-Only Enforcement
  As a security administrator
  I want audit and error events to be impossible to update or delete
  So that the trail is tamper-evident at the application layer

  Scenario: Updating a persisted audit event is rejected
    Given the append-only guard is registered
    And an audit event row is persisted
    When the audit event row is updated
    Then an AppendOnlyViolationError is raised
    And the error names the event and the "update" mutation

  Scenario: Deleting a persisted audit event is rejected
    Given the append-only guard is registered
    And an audit event row is persisted
    When the audit event row is deleted
    Then an AppendOnlyViolationError is raised
    And the error names the event and the "delete" mutation

  Scenario: Updating a persisted error event is rejected
    Given the append-only guard is registered
    And an error event row is persisted
    When the error event row is updated
    Then an AppendOnlyViolationError is raised
    And the error names the event and the "update" mutation

  Scenario: Deleting a persisted error event is rejected
    Given the append-only guard is registered
    And an error event row is persisted
    When the error event row is deleted
    Then an AppendOnlyViolationError is raised
    And the error names the event and the "delete" mutation

  Scenario: Appending a new audit event is not blocked
    Given the append-only guard is registered
    When a new audit event row is appended
    Then the row is persisted without an AppendOnlyViolationError
