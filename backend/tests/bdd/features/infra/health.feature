Feature: Infrastructure health checks
  As an operator
  I want liveness and readiness endpoints
  So that deployment health monitoring reflects dependency status

  Scenario: Liveness returns ok and is advisory
    When I GET the liveness endpoint /healthz
    Then the response status is 200
    And the liveness body is {"status": "ok"}

  Scenario: Readiness is ok when every gate passes
    Given the readiness probe is set to healthy
    When I GET the readiness endpoint /healthz/ready
    Then the response status is 200
    And the readiness overall status is "ok"

  Scenario: Readiness reports degraded without flipping to unavailable
    Given the redis check is degraded
    When I GET the readiness endpoint /healthz/ready
    Then the response status is 200
    And the readiness overall status is "degraded"
    And the check "redis" has status "degraded"

  Scenario: Readiness returns 503 when a gate is unavailable
    Given the database check is unavailable
    When I GET the readiness endpoint /healthz/ready
    Then the response status is 503
    And the readiness overall status is "unavailable"
    And the check "database" has status "unavailable"

  Scenario: Dispatcher reconcile unavailable gates readiness
    Given the dispatcher reconcile check is unavailable
    When I GET the readiness endpoint /healthz/ready
    Then the response status is 503
    And the readiness overall status is "unavailable"
    And the check "dispatcher_reconcile" has status "unavailable"

  Scenario: Dispatcher reconcile degraded stays advisory
    Given the dispatcher reconcile check is degraded
    When I GET the readiness endpoint /healthz/ready
    Then the response status is 200
    And the readiness overall status is "ok"
    And the check "dispatcher_reconcile" has status "degraded"

  Scenario: Every readiness check carries status, latency and detail
    Given the readiness probe is set to healthy
    When I GET the readiness endpoint /healthz/ready
    Then the response status is 200
    And every check has status, latency_ms and detail fields