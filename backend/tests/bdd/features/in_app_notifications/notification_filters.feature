Feature: Notification Filters
  As a user
  I want to filter notifications on the notifications page
  So that I can find relevant information quickly

  Background:
    Given the user has notifications at various levels, scopes, and categories

  Scenario: Filter by level
    When the user selects level filter "error"
    Then only error-level notifications are shown

  Scenario: Filter by scope
    When the user selects scope filter "admin"
    Then only admin-scoped notifications are shown

  Scenario: Filter by category
    When the user selects category filter "run.failed"
    Then only "run.failed" category notifications are shown

  Scenario: Filter by status — active
    When the user selects status filter "active"
    Then only non-dismissed notifications are shown

  Scenario: Filter by status — dismissed_self
    When the user selects status filter "dismissed_self"
    Then only self-dismissed notifications are shown

  Scenario: Multiple filters stack
    When the user selects level filter "warning"
    And selects scope filter "org"
    Then only org-wide warning notifications are shown

  Scenario: Empty state with filters
    Given no notifications match the current filters
    Then the page shows "No notifications matching your filters"
    And shows a "Clear filters" action

  Scenario: Pagination respects filters
    Given the user has 50 filtered notifications
    When they set page size to 20
    And navigate to page 2
    Then they see notifications 21-40 matching the filters
