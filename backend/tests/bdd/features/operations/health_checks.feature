Feature: Health Checks

  As a platform operator
  I want liveness and readiness endpoints
  So that deployment orchestrators can monitor and safely fail-over machines

  Scenario: Liveness endpoint returns OK
    Given the application is running
    When I GET /healthz
    Then the response status is 200
    And the response body status is "ok"

  Scenario: Readiness is OK when every dependency is healthy
    Given the database check is ok
    And the redis check is ok
    And the checkpointer check is ok
    And the migrations check is ok
    And the saq workers check is ok
    And the system cron check is ok
    And the dispatcher reconcile check is ok
    When I GET /healthz/ready
    Then the response status is 200
    And the overall readiness status is "ok"
    And the readiness response reports every non-advisory check as "ok"

  Scenario: Readiness is degraded when a non-critical dependency degrades
    Given the database check is ok
    And the redis check is degraded
    And the checkpointer check is ok
    And the migrations check is ok
    And the saq workers check is ok
    And the system cron check is ok
    And the dispatcher reconcile check is ok
    When I GET /healthz/ready
    Then the response status is 200
    And the overall readiness status is "degraded"

  Scenario: Readiness is unavailable when a critical dependency is down
    Given the database check is unavailable
    And the redis check is ok
    And the checkpointer check is ok
    And the migrations check is ok
    And the saq workers check is ok
    And the system cron check is ok
    And the dispatcher reconcile check is ok
    When I GET /healthz/ready
    Then the response status is 503
    And the overall readiness status is "unavailable"

  Scenario: A stale dispatcher reconcile gates readiness as unavailable
    Given the database check is ok
    And the redis check is ok
    And the checkpointer check is ok
    And the migrations check is ok
    And the saq workers check is ok
    And the system cron check is ok
    And the dispatcher reconcile check is unavailable
    When I GET /healthz/ready
    Then the response status is 503
    And the overall readiness status is "unavailable"

  Scenario: A single missed dispatcher reconcile tick stays advisory
    Given the database check is ok
    And the redis check is ok
    And the checkpointer check is ok
    And the migrations check is ok
    And the saq workers check is ok
    And the system cron check is ok
    And the dispatcher reconcile check is degraded
    When I GET /healthz/ready
    Then the response status is 200
    And the overall readiness status is "ok"
