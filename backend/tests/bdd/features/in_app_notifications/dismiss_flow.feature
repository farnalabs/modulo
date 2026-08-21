Feature: Notification Dismiss Flow
  As a user
  I want to dismiss notifications with appropriate scope
  So that I control what I see and what others see

  Background:
    Given the user has a notification with dismiss_strategy "user_only"

  Scenario: Self-dismiss removes for current user only
    When the user dismisses the notification for themselves
    Then the notification is hidden for this user
    And other users still see the notification

  Scenario: Scope dismiss is blocked when dismiss_strategy is "user_only"
    When the user tries to dismiss the notification for all users
    Then the request is rejected with 400
    And the notification remains visible

  Scenario: Admin can dismiss org-wide when dismiss_strategy is "org_admin"
    Given the user is an admin
    And the notification has dismiss_strategy "org_admin"
    When the user dismisses the notification for the org
    Then the notification is hidden for all org members

  Scenario: Non-admin scope dismiss is blocked when dismiss_strategy is "org_admin"
    Given the user is not an admin
    And the notification has dismiss_strategy "org_admin"
    When the user tries to dismiss the notification for the org
    Then the request is rejected with 400

  Scenario: Scope dismiss is allowed for any user with "any_scope" strategy
    Given the notification has dismiss_strategy "any_scope"
    When the user dismisses the notification for all users
    Then the notification is hidden for all org members
    And the dismiss is logged in the audit trail

  Scenario: Dismiss dialog shows scope choices based on notification config
    Given a notification with dismissible_at_scope = true
    When the user clicks "Dismiss"
    Then the dialog shows "Dismiss for me" option
    And the dialog shows a scope dismiss option

  Scenario: Dismiss dialog hides scope choice for user-only notifications
    Given a notification with dismissible_at_scope = false
    When the user clicks "Dismiss"
    Then the dialog shows only "Dismiss for me" option
