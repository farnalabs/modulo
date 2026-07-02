Feature: In-App Notification SSE Integration
  As a user
  I want to receive real-time notification updates via SSE
  So that I see new notifications without refreshing

  Scenario: New notification appears via SSE push
    Given the user is on the dashboard
    When a new notification is created for the org
    Then the dashboard notifications panel refreshes
    And the new notification appears in the list

  Scenario: Scope-level dismiss is broadcast to other users
    Given two users in the same org
    And both see the same org-scope notification
    When user A dismisses the notification for everyone
    Then user B's dashboard refreshes
    And the notification is removed from user B's dashboard

  Scenario: Notification bell badge updates via SSE
    Given the user has no unread notifications
    When a new error notification is created
    Then the notification bell badge shows "1"

  Scenario: Self-dismiss does not affect other users
    Given two users in the same org
    And both see the same notification
    When user A dismisses the notification for themselves
    Then user B still sees the notification
