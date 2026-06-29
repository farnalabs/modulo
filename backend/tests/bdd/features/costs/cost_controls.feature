Feature: Cost Controls
  As an admin
  I want to enforce token budgets, spend limits, and circuit breakers
  So that I can control costs and prevent runaway spending

  Background:
    Given I am authenticated as an admin in org "acme"

  # ── Token budget (future: per-agent hard stop) ─────────────────────────────

  Scenario: Token budget enforced
    Given agent "code-writer" has a token budget of 100000 tokens
    And a run is in progress for agent "code-writer"
    When the run accumulates 110000 tokens
    Then the run transitions to "budget_exceeded" terminal state
    And the error message is "This run exceeded its token budget."

  # ── Spend limits (implemented) ────────────────────────────────────────────

  Scenario: Org spend limit reached blocks new runs
    Given org "acme" has a daily spend limit of $100.00
    And org "acme" has already spent $95.00 today
    When a new run costs $10.00
    Then the spend is rejected with reason "Daily spend limit exceeded for organisation"
    And the org run count is not incremented

  Scenario: Per-team spend limit enforced independently
    Given org "acme" has a daily spend limit of $500.00
    And team "alpha" has a daily spend limit of $50.00
    And team "alpha" has already spent $45.00 today
    When a new run for team "alpha" costs $10.00
    Then the spend is rejected with reason "Daily spend limit exceeded for team"
    And the org run count is not incremented

  Scenario: Spend under both limits is approved
    Given org "acme" has a daily spend limit of $500.00
    And team "beta" has a daily spend limit of $100.00
    And org "acme" has spent $100.00 today
    And team "beta" has spent $20.00 today
    When a new run for team "beta" costs $30.00
    Then the spend is approved
    And the org run count is incremented
    And the team run count is incremented

  # ── Circuit breaker (future: per-pipeline permanent pause) ────────────────

  Scenario: Circuit breaker trips when pipeline exceeds spend threshold
    Given pipeline "data-pipeline" has a circuit breaker threshold of $1000.00
    And pipeline "data-pipeline" has accumulated $950.00 this month
    When the pipeline accumulates another $100.00
    Then the circuit breaker trips
    And the pipeline trigger is permanently paused
    And an admin notification is sent

  Scenario: Circuit breaker resets after admin re-enables
    Given pipeline "data-pipeline" has a tripped circuit breaker
    When an admin re-enables pipeline "data-pipeline"
    Then the circuit breaker is reset
    And new runs are allowed

  # ── Admin API (implemented) ───────────────────────────────────────────────

  Scenario: Admin sets org spend limit
    Given I am authenticated as an admin in org "acme"
    When I PUT /api/v1/admin/costs/limits/org with daily spend limit $250.00
    Then the response status is 200
    And the response contains daily_spend_limit of 250.0

  Scenario: Admin sets team spend limit
    Given org "acme" has team "alpha" with id "10000000-0000-0000-0000-000000000001"
    When I PUT /api/v1/admin/costs/limits/teams/10000000-0000-0000-0000-000000000001 with daily spend limit $75.00
    Then the response status is 200
    And the response contains daily_spend_limit of 75.0

  Scenario: View current cost report
    Given org "acme" has cost data for this month
    When I GET /api/v1/admin/costs
    Then the response status is 200
    And the response contains period "month"
    And the response contains group_by "team"
    And the response contains spend items

  Scenario: View cost report by org
    When I GET /api/v1/admin/costs with group_by "org" and period "week"
    Then the response status is 200
    And the response contains period "week"
    And the response contains group_by "org"
    And the response contains a single org-level item

  Scenario: Non-admin is rejected from cost endpoints
    Given I am authenticated as a viewer in org "acme"
    When I GET /api/v1/admin/costs
    Then the response status is 403
