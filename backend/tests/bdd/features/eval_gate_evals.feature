Feature: Eval Gate Evals
  As a pipeline operator
  I want eval results to be persisted before block/warn decisions
  So that failing evals are never silently lost

  Scenario: Blocking eval persists result before halting
    Given a pipeline "eval-block-pipeline" with evals:
      | name      | eval_type | failure_behaviour |
      | pass-eval | regex     | warn              |
      | block-eval| regex     | block             |
    And a pipeline run of "eval-block-pipeline"
    And eval "pass-eval" produces passed=true
    And eval "block-eval" produces passed=false
    When the pipeline run completes
    Then the run terminal status is "eval_failed"
    And the run error code is "eval_blocked"
    And an EvalResult row exists for eval "block-eval" with passed=false

  Scenario: Warn eval persists result and run continues
    Given a pipeline "eval-warn-pipeline" with evals:
      | name     | eval_type | failure_behaviour |
      | warn-eval| regex     | warn              |
    And a pipeline run of "eval-warn-pipeline"
    And eval "warn-eval" produces passed=false
    When the pipeline run completes
    Then the run terminal status is "completed"
    And an EvalResult row exists for eval "warn-eval" with passed=false
