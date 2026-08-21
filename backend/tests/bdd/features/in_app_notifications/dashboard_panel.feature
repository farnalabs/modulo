Feature: In-App Dashboard Notification Panel
  As a user
  I want to see recent notifications on the dashboard
  So that I can stay informed without navigating away

  Scenario: Dashboard shows up to 5 most recent notifications
    Given the user has 10 active notifications
    When they view the dashboard
    Then they see at most 5 notifications
    And the notifications are ordered by most recent first

  Scenario: Dashboard respects user level filter
    Given the user has dashboard level filter set to "warning"
    And they have notifications at "info", "warning", and "error" levels
    When they view the dashboard
    Then they see only "warning" and "error" notifications

  Scenario: Review Later hides from dashboard but keeps in list
    Given a notification is visible on the dashboard
    When the user clicks "Review Later" on that notification
    Then the notification is removed from the dashboard
    And the notification is still visible on the notifications page

  Scenario: Dismiss removes from both dashboard and notifications page
    Given a notification is visible on the dashboard
    When the user dismisses the notification for themselves
    Then the notification is removed from the dashboard
    And the notification is not visible on the notifications page

  Scenario: Collapsed state is persisted
    When the user collapses the notifications panel
    And refreshes the page
    Then the notifications panel is still collapsed

  Scenario: Dashboard shows "View all" link when more than 5 notifications exist
    Given the user has 6 active notifications
    When they view the dashboard
    Then they see a "View all" link
    And the link routes to /notifications
