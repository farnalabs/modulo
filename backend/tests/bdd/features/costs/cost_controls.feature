Feature: Cost Controls
  As an admin
  I want to enforce token budgets, spend limits, and circuit breakers
  So that I can control costs and prevent runaway spending

  Background:
    Given I am authenticated as an admin in org "acme"

  # ── Token budget (implemented: per-agent hard stop) ────────────────────────

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
    Then the spend is rejected with reason "daily_limit_exceeded: organisation"
    And the org run count is not incremented

  Scenario: Per-team spend limit enforced independently
    Given org "acme" has a daily spend limit of $500.00
    And team "alpha" has a daily spend limit of $50.00
    And team "alpha" has already spent $45.00 today
    When a new run for team "alpha" costs $10.00
    Then the spend is rejected with reason "daily_limit_exceeded: team"
    And the org run count is not incremented

  Scenario: Spend under both limits is approved
    Given org "acme" has a daily spend limit of $500.00
    And team "beta" has a daily spend limit of $100.00
    And org "acme" has already spent $100.00 today
    And team "beta" has already spent $20.00 today
    When a new run for team "beta" costs $30.00
    Then the spend is approved
    And the org run count is incremented
    And the team run count is incremented

  # ── Circuit breaker (implemented: per-pipeline permanent pause) ────────────

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

  # ── Hard spend ceilings (FAR-391, dedicated /ceiling surface) ──────────────

  Scenario: Admin reads the org spend ceiling and remaining budget
    Given org "acme" has cost ceilings with max_run_cost $5.00 and spend_ceiling $100.00
    And org "acme" has consumed $25.00 of its ceiling
    When I GET /api/v1/admin/costs/ceiling
    Then the response status is 200
    And the response contains spend_ceiling of 100.0
    And the response contains max_run_cost of 5.0
    And the response contains remaining_budget_usd of 75.0

  Scenario: Admin sets a fresh org spend ceiling
    When I PUT /api/v1/admin/costs/ceiling with spend_ceiling $100.00
    Then the response status is 200
    And the response contains spend_ceiling of 100.0
    And the response ceiling was stored as 10000 cents

  Scenario: Explicit null clears one ceiling and preserves the other
    Given org "acme" has cost ceilings with max_run_cost $5.00 and spend_ceiling $100.00
    When I PUT /api/v1/admin/costs/ceiling with spend_ceiling null
    Then the response status is 200
    And the response contains max_run_cost of 5.0
    And the response spend_ceiling is null

  Scenario: A negative ceiling value is rejected
    When I PUT /api/v1/admin/costs/ceiling with an invalid negative ceiling
    Then the response status is 422

  # ── Ceiling enforcement at run finalize (hard stop, never billed beyond) ───

  Scenario: Run above the org ceiling is refused and halts
    Given org "acme" has a spend ceiling of $1.00 and has consumed it all
    When a run with cost $2.00 is finalized
    Then the run terminalizes as "cost_ceiling_exceeded"
    And the refusal reason is "org_spend_ceiling_exceeded"
    And the org cumulative spend is not incremented

  Scenario: Run above the per-run ceiling is refused
    Given org "acme" has a per-run ceiling of $1.00
    When a run with cost $2.00 is finalized
    Then the run terminalizes as "cost_ceiling_exceeded"
    And the refusal reason is "run_cost_ceiling_exceeded"

  Scenario: Run within ceilings increments the org cumulative spend
    Given org "acme" has cost ceilings with max_run_cost $5.00 and spend_ceiling $100.00
    And org "acme" has consumed $5.00 of its ceiling
    When a run with cost $3.00 is finalized
    Then the run ledger is accepted
    And the org cumulative spend is incremented by $3.00

  # ── Scheduled cost reports (/reports) ──────────────────────────────────────

  Scenario: Admin creates a scheduled cost report
    When I POST /api/v1/admin/costs/reports with a weekly team CSV report
    Then the response status is 201
    And the response contains report id and period "weekly"

  Scenario: Scheduled report requires at least one recipient
    When I POST /api/v1/admin/costs/reports without recipients
    Then the response status is 422

  Scenario: Admin lists scheduled cost reports
    Given org "acme" has a scheduled weekly report
    When I GET /api/v1/admin/costs/reports
    Then the response status is 200
    And the response contains one scheduled report

  Scenario: Admin deletes a scheduled cost report
    Given org "acme" has a scheduled weekly report with id "30000000-0000-0000-0000-000000000001"
    When I DELETE /api/v1/admin/costs/reports/30000000-0000-0000-0000-000000000001
    Then the response status is 204

  Scenario: Deleting a missing scheduled report is 404
    When I DELETE /api/v1/admin/costs/reports/30000000-0000-0000-0000-000000000099
    Then the response status is 404

  # ── Spend anomaly detection (/anomalies) ───────────────────────────────────

  Scenario: A spend spike is surfaced as a dismissible anomaly
    Given org "acme" has a detected spend anomaly of $5.00 against a $1.00 baseline
    When I GET /api/v1/admin/costs/anomalies
    Then the response status is 200
    And the response contains one fresh anomaly
    And the anomaly carries a persisted id
    When I dismiss the reported anomaly
    Then the response status is 204

  Scenario: Dismissing a missing anomaly is 404
    When I POST /api/v1/admin/costs/anomalies/dismiss/40000000-0000-0000-0000-000000000099
    Then the response status is 404

  # ── Cost components (/api/v1/admin/costs/components) ───────────────────────

  Scenario: Admin creates a cost component
    When I POST /api/v1/admin/costs/components with a reportable self_reported component
    Then the response status is 201
    And the response contains component name "reported_cost"

  Scenario: Duplicate cost component names are rejected
    Given a cost component named "llm_tokens" already exists
    When I POST /api/v1/admin/costs/components named "llm_tokens"
    Then the response status is 409

  Scenario: A self_reported component cannot carry a formula
    When I POST /api/v1/admin/costs/components with a self_reported component that has a formula
    Then the response status is 422

  Scenario: Admin lists cost components
    Given org "acme" has cost components configured
    When I GET /api/v1/admin/costs/components
    Then the response status is 200
    And the response contains the configured components

  Scenario: Admin deletes a cost component
    Given a cost component with id "50000000-0000-0000-0000-000000000001"
    When I DELETE /api/v1/admin/costs/components/50000000-0000-0000-0000-000000000001
    Then the response status is 204
