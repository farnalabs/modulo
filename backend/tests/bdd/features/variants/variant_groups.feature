Feature: Variant Groups — Weighted Multi-Run, Comparison, and Eval Coverage
  As a pipeline operator
  I want to batch-run variants by weight distribution, compare results side-by-side,
  and detect eval coverage gaps
  So that I can A/B test pipeline configurations at scale

  @awaiting-implementation
  Scenario: Weighted batch run distributes N runs by variant weights
    Given a variant group "ab-test-1" configured for pipeline "deploy-service"
    And the group has weighted variants "control" (70) and "experiment" (30)
    When a batch of 100 runs is triggered on the variant group
    Then 100 runs are created across the variants
    And the control variant receives approximately 70 runs
    And the experiment variant receives approximately 30 runs

  @awaiting-implementation
  Scenario: Sequential execution order matches insertion order
    Given a variant group "seq-test" configured for pipeline "deploy-service"
    And the group uses sequential strategy with variants "step-a" and "step-b"
    When a sequential run is triggered on the variant group
    Then runs are created in variant insertion order
    And the first run has variant_name "step-a"
    And the second run has variant_name "step-b"

  @awaiting-implementation
  Scenario: Variant comparison returns eval scores per node and token cost
    Given a variant group "compare-test" configured for pipeline "deploy-service"
    And both variants have completed runs with eval and token data
    When the comparison view is requested for the variant group
    Then the comparison includes eval scores per node for each variant
    And the comparison includes per-variant token cost

  @awaiting-implementation
  Scenario: Eval coverage gap is detected when variants diverge but evals match
    Given a variant group "cover-test" configured for pipeline "deploy-service"
    And the group has variants with divergent outputs and identical eval scores
    When the eval coverage signal is requested for the variant group
    Then a coverage_warning is included in the response
    And the warning says "Variants diverged but evals did not differentiate"

  @awaiting-implementation
  Scenario: Comparison shows token cost breakdown per variant
    Given a variant group "cost-test" configured for pipeline "deploy-service"
    And both variants have completed runs with eval and token data
    When the comparison view is requested for the variant group
    Then each variant entry includes input_tokens and output_tokens in token_cost
    And the total cost differs between variants

  Scenario: Zero-weight variant is not selected in weighted mode
    Given a variant group "zero-test" configured for pipeline "deploy-service"
    And the group has weighted variants "control" (100) and "disabled" (0)
    When a single run is triggered on the variant group
    Then the selected variant is "control"

  @awaiting-implementation
  Scenario: Batch run is rejected when quota is exceeded
    Given a variant group "quota-test" configured for pipeline "deploy-service"
    And the group has max_concurrent_runs set to 0
    When a batch of 5 runs is triggered on the variant group
    Then the batch is rejected with a quota_exceeded error
