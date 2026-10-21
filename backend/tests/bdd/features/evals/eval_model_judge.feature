Feature: Model judge Eval
  As a pipeline author
  I want to evaluate agent output using an LLM-as-judge with a rubric
  So that subjective quality criteria are scored automatically

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Rubric-based scoring passes above threshold
    Given node "code-reviewer" has an model_judge scorer eval "quality-check"
    And the eval config has rubric with criteria "correctness, style, efficiency"
    And the eval has pass_threshold 0.7
    And the eval has failure_behaviour "warn"
    When the node outputs {"code": "def foo(): pass"}
    And the model_judge scorer callable returns {"passed": true, "score": 0.85, "detail": "Meets criteria"}
    Then the eval result has passed true
    And the eval result has score 0.85

  Scenario: Model judge score below threshold fails the eval
    Given node "code-reviewer" has an model_judge scorer eval "quality-check"
    And the eval has pass_threshold 0.7
    And the eval has failure_behaviour "warn"
    When the node outputs {"code": "def foo(): pass"}
    And the model_judge scorer callable returns {"passed": false, "score": 0.45, "detail": "Missing error handling"}
    Then the eval result has passed false
    And the eval result has score 0.45

  Scenario: Model judge uses a dedicated model backend
    Given node "code-reviewer" has an model_judge scorer eval "quality-check"
    And the eval config specifies model_backend_id "judge-gpt4o"
    When the node outputs {"code": "def foo(): pass"}
    And the eval engine invokes the model_judge callable
    Then the callable receives model_backend_id "judge-gpt4o"
    And the callable does not use the agent's own model backend

  Scenario: Custom rubric prompt is sent to the judge model
    Given node "code-reviewer" has an model_judge scorer eval "quality-check"
    And the eval config has rubric_prompt "Evaluate the following code for correctness, style, and efficiency. Score 0-1."
    When the node outputs {"code": "def foo(): pass"}
    And the eval engine invokes the model_judge callable
    Then the callable receives the rubric_prompt in its input
    And the prompt treats agent output as untrusted

  Scenario: Model judge with no callable returns failed
    Given node "code-reviewer" has an model_judge scorer eval "quality-check"
    When the node outputs {"code": "def foo(): pass"}
    And no model_judge callable is configured
    Then the eval result has passed false
    And the eval result has score 0.0

  Scenario: Model judge block behaviour stops the pipeline
    Given node "code-reviewer" has an model_judge scorer eval "quality-check"
    And the eval has pass_threshold 0.7
    And the eval has failure_behaviour "block"
    When the node outputs {"code": "def foo(): pass"}
    And the model_judge scorer callable returns {"passed": false, "score": 0.3, "detail": "Critical issue"}
    Then an EvalBlockedError is raised
    And the run transitions to status "eval_failed"
