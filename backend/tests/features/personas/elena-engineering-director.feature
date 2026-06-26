Feature: Elena — VP / Director of Engineering
  As Elena, VP Engineering responsible for delivery velocity and quality
  I want visibility into how agentic delivery is performing across my teams
  So that I can make data-driven decisions about automation investment

  @goal-elena-org-dashboard @delivered
  Scenario: Elena sees a consolidated org dashboard
    Given my org has 4 teams each with 2-3 active pipelines
    When I navigate to the org dashboard
    Then I see total runs this week, avg eval pass rate, and active pipeline count
    And I see a breakdown by team
    And I see a trend chart over the last 30 days

  @goal-elena-team-comparison @delivered
  Scenario: Elena compares eval pass rates across teams
    Given team "alpha" has 92% eval pass rate
    And team "beta" has 74% eval pass rate
    When I open the team comparison view
    Then I can see pass rates side by side
    And I can drill into team "beta" to see which pipelines are dragging the average

  @goal-elena-cost-by-team @delivered
  Scenario: Elena sees token spend by team and pipeline
    Given team "alpha" has token spend of $240 this month
    And team "beta" has token spend of $180 this month
    When I open the cost breakdown
    Then I see total spend per team
    And I see spend per pipeline within each team
    And I see average cost per run

  @goal-elena-eval-trend @delivered
  Scenario: Elena spots a quality regression in her eval dashboard
    Given pipeline "prd-to-tickets" had 90% eval pass rate last month
    And this month the rate dropped to 72%
    When I view the eval dashboard
    Then the regression is flagged with an alert
    And I can drill into the affected eval cases
    And I see which schema or prompt version changed

  @goal-elena-complexity-warning
  Scenario: Elena is warned when a pipeline grows too complex
    Given a pipeline has grown from 5 to 15 nodes with unstructured prompts
    When the complexity reviewer runs
    Then the pipeline is flagged with a complexity warning
    And the warning recommends splitting into sub-pipelines
    And the warning is visible on the pipeline overview page

  @goal-elena-ab-test-results @delivered
  Scenario: Elena decides between models based on eval comparison
    Given pipeline "code-review" ran variant A (Claude) and variant B (GPT-4o)
    When I open the variant comparison view
    Then I see eval scores for both variants
    And I see token cost for both variants
    And I can promote one variant as the new default

  @goal-elena-quality-report @delivered
  Scenario: Elena receives a weekly quality report via Slack
    Given a scheduled quality report is configured
    When the report runs every Monday
    Then the report contains weekly run volume, eval pass rate, and cost summary
    And the report includes changes from the previous week
    And the report is posted to the configured Slack channel

  @goal-elena-run-inspection
  Scenario: Elena drills from dashboard into a specific run
    Given the dashboard shows a quality dip on pipeline "ticket-writer"
    When I click on the affected pipeline
    Then I see a list of recent runs with eval scores
    When I click on a specific run
    Then I see per-node status and eval results
    And I see the agent outputs for each node

  @goal-elena-okr-alignment @delivered
  Scenario: Elena aligns eval suites with team OKRs
    Given team "alpha" has an OKR for "improve ticket quality"
    When I create an eval suite with cases covering ticket completeness
    And I set pass_threshold to 0.85
    Then the eval suite runs on every ticket-writing pipeline
    And I can track OKR progress via the eval pass rate trend

  @goal-elena-human-effort-trend @delivered
  Scenario: Elena sees whether HITL effort is decreasing over time
    Given 3 months of run data with HITL decisions
    When I view the human effort trend chart
    Then I see HITL volume per week
    And I see average time-to-approve
    And I see rejection rate trend
    And I can correlate with eval pass rate changes
