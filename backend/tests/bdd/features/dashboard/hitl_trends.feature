Feature: HITL Effort Trends
  As a platform operator
  I want to view HITL decision volume, rejection rates, and review-time trends
  So that I can monitor HITL governance effectiveness

  Scenario: View HITL volume over 7 days
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/trends?days=7
    Then the response status is 200
    And the response contains hitl_volume with 7 entries
    And each hitl_volume entry has total_decisions, approved_count, rejected_count, rejection_rate, and avg_time_to_approve_ms

  Scenario: View rejection trend with rolling average
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/trends?days=7
    Then the response status is 200
    And the response contains rejection_trend with 7 entries
    And each rejection_trend entry has rolling_rejection_rate and raw_rejection_rate

  Scenario: View correlation between rejection rate and eval pass rate
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/trends?days=30
    Then the response status is 200
    And the response contains correlation with 30 entries
    And each correlation entry has rejection_rate and eval_pass_rate

  Scenario: View feedback volume
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/trends?days=7
    Then the response status is 200
    And the response contains feedback_volume with 7 entries
    And each feedback_volume entry has feedback_count, resolved_count, and correcting_count

  Scenario: All trend arrays align by day count
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/trends?days=7
    Then the response status is 200
    And hitl_volume, rejection_trend, correlation, and feedback_volume all have the same length

  Scenario: Empty period returns zero-filled arrays
    Given I am authenticated as an admin
    And there are no HITL decisions in the selected period
    When I request GET /api/v1/dashboard/trends?days=7
    Then the response status is 200
    And every hitl_volume entry has total_decisions=0 and rejection_rate=0.0

  Scenario: Rejects invalid days parameter
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/trends?days=0
    Then the response status is 422
