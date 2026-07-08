Feature: Conditional HITL Gating
  As a pipeline author
  I want HITL gates to fire only when eval results fall below a threshold
  So that low-quality output requires human review but good output flows through

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Eval score below threshold triggers HITL interrupt
    Given node "content-generator" has an llm_judge eval "quality-check"
    And the edge after "content-generator" has a HITL gate
    And the gate condition references eval "quality-check" with threshold 0.8 operator "lt"
    When the node outputs {"content": "Draft document"}
    And the llm_judge callable returns {"passed": false, "score": 0.45, "detail": "Poor quality"}
    Then the gate condition evaluates to true
    And a NodeInterrupt is raised
    And the run transitions to "awaiting_human"

  Scenario: Eval score above threshold skips the HITL gate
    Given node "content-generator" has an llm_judge eval "quality-check"
    And the edge after "content-generator" has a HITL gate
    And the gate condition references eval "quality-check" with threshold 0.8 operator "lt"
    When the node outputs {"content": "Well-written document"}
    And the llm_judge callable returns {"passed": true, "score": 0.92, "detail": "High quality"}
    Then the gate condition evaluates to false
    And execution continues without interrupting
    And the gate artifact contains "condition_skipped"

  # NOTE: Using bare `run_context.draft_mode` instead of
  # `run_context.draft_mode == true` because JMESPath Python library's ==
  # operator with boolean literals (`true`, `false`) does not compare correctly
  # against Python bool values. This is a known JMESPath limitation.
  Scenario: JMESPath condition on gate state skips the gate
    Given the edge after "content-generator" has a HITL gate
    And the gate has a JMESPath condition "run_context.draft_mode"
    When the run_context has draft_mode false
    And the run reaches the gate
    Then the JMESPath condition evaluates to false
    And the gate is skipped
    And no interrupt is raised

  Scenario: JMESPath condition on gate state triggers the gate
    Given the edge after "content-generator" has a HITL gate
    And the gate has a JMESPath condition "run_context.draft_mode"
    When the run_context has draft_mode true
    And the run reaches the gate
    Then the JMESPath condition evaluates to true
    And the gate proceeds to eval checks

  Scenario: Eval block failure takes priority over HITL interrupt
    Given node "content-generator" has an llm_judge eval "quality-check"
    And the eval has failure_behaviour "block"
    And the edge after "content-generator" has a HITL gate
    And the gate condition references eval "quality-check" with threshold 0.8 operator "lt"
    When the node outputs {"content": "Insecure content"}
    And the llm_judge callable returns {"passed": false, "score": 0.1, "detail": "Security violation"}
    Then EvalBlockedError is raised
    And the run transitions to "eval_failed"
    And no HITL interrupt is raised

  Scenario: Gate resumes from interrupt after eval condition met on resume
    Given a run is waiting at gate "review-gate" due to low eval score
    When a human approves the gate
    And the run resumes
    Then the gate does not re-evaluate the condition
    And the gate does not re-run evals
    And execution continues to the next node

  Scenario: Multiple evals in gate condition with any operator
    Given node "content-generator" has evals "quality-check, safety-check"
    And the edge after "content-generator" has a HITL gate
    And the gate condition references eval "quality-check" with threshold 0.7 operator "lt"
    When the node outputs {"content": "Draft document"}
    And "quality-check" scores 0.6
    And "safety-check" scores 1.0
    Then the gate condition on "quality-check" evaluates to true
    And a NodeInterrupt is raised

  Scenario: Reject routing from conditional HITL gate
    Given node "content-generator" has an llm_judge eval "quality-check"
    And the edge after "content-generator" has a HITL gate
    And the gate condition references eval "quality-check" with threshold 0.8 operator "lt"
    And the gate has reject_target "human-fix-node"
    When a human rejects the gate
    Then the run routes to "human-fix-node"
    And the gate artifact shows action "rejected"
