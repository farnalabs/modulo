Feature: Eval Gate Enforcement
  As a pipeline operator
  I want eval gates to either block the pipeline or warn on failure
  So that I can control quality enforcement severity

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Block behaviour raises EvalBlockedError when regex does not match
    Given node "code-reviewer" has a regex eval "no-secrets"
    And the eval config has pattern "API_KEY|SECRET"
    And the eval config has field "output"
    And the eval has failure_behaviour "block"
    When the node outputs {"output": "Use config from env"}
    And the eval engine evaluates the output
    Then an EvalBlockedError is raised with detail "no-secrets"
    And the run status transitions to "eval_failed"

  Scenario: Block behaviour transitions run to eval_failed
    Given node "code-reviewer" has a regex eval "no-secrets"
    And the eval has failure_behaviour "block"
    When the eval engine raises EvalBlockedError
    Then the pipeline executor catches the error
    And the run status transitions to "eval_failed"
    And the error_code is "eval_blocked"

  Scenario: Warn behaviour logs but does not halt when regex does not match
    Given node "code-reviewer" has a regex eval "no-secrets"
    And the eval config has pattern "API_KEY|SECRET"
    And the eval config has field "output"
    And the eval has failure_behaviour "warn"
    When the node outputs {"output": "Use config from env"}
    And the eval engine evaluates the output
    Then a warning is logged
    And pipeline execution continues
    And the run does not transition to "eval_failed"

  Scenario: Suite-level pass threshold blocks on aggregate
    Given pipeline "quality-pipe" has eval suite "release-suite"
    And the suite has pass_threshold 0.8
    And 3 of 5 evals pass
    And the aggregate score is 0.6
    When the run completes
    Then the final status is "failed"
    And the error_code is "eval_suite_blocked"

  Scenario: Suite-level pass threshold passes on aggregate
    Given pipeline "quality-pipe" has eval suite "release-suite"
    And the suite has pass_threshold 0.8
    And 5 of 5 evals pass
    And the aggregate score is 0.92
    When the run completes
    Then the final status is "complete"
    And no suite-level error is raised

  Scenario: Block failure is recorded in AuditEvent
    Given node "code-reviewer" has a regex eval "no-secrets"
    And the eval has failure_behaviour "block"
    When the eval engine raises EvalBlockedError
    Then an AuditEvent is written
    And the event type is "eval_blocked"
    And the event includes the eval name and detail

  Scenario: Multiple evals on one node all must pass
    Given node "code-reviewer" has evals "no-secrets, has-tests, style-check"
    And all three evals have failure_behaviour "block"
    When the node outputs {"output": "def foo(): pass"}
    And eval "no-secrets" passes
    And eval "has-tests" fails
    Then EvalBlockedError is raised on "has-tests"
    And remaining evals are not evaluated
