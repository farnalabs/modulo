Feature: Conditional Transitions
  As a pipeline author
  I want conditional edges, kick-back routing, and parallel branches in my pipelines
  So that execution flow adapts to runtime state without manual intervention

  Background:
    Given I am authenticated in org "acme"

  Scenario: Conditional edge routes based on state
    Given pipeline "decision-pipeline" has a conditional router at node "classifier"
    And the router has a condition "artifacts[0].status == 'passed'" routing to "promote"
    And the router has a condition "artifacts[0].status == 'failed'" routing to "rollback"
    When the run reaches "classifier" with state containing artifact status "passed"
    Then the run routes to "promote"
    And the run does not visit "rollback"

  Scenario: First matching condition wins
    Given pipeline "triage-pipeline" has a conditional router at node "triage"
    And the router has a condition "artifacts[0].severity > `8`" routing to "critical-path"
    And the router has a condition "artifacts[0].severity > `3`" routing to "normal-path"
    And the router has a condition "true" routing to "default-path"
    When the run reaches "triage" with artifact severity 9
    Then the run routes to "critical-path"
    And the run does not visit "normal-path" or "default-path"

  Scenario: No condition matches — uses normal fallback
    Given pipeline "fallback-pipeline" has a conditional router at node "decider"
    And the router has a condition "artifacts[0].env == 'production'" routing to "prod-deploy"
    And the router has a normal fallback edge to "staging-deploy"
    When the run reaches "decider" with artifact env "development"
    Then the run routes to "staging-deploy"

  Scenario: Default target when no normal edges
    Given pipeline "catchall-pipeline" has a conditional router at node "router"
    And the router has conditions without any normal edges
    And a conditional edge specifies default_target "catchall"
    When the run reaches "router" with state matching no conditions
    Then the run routes to "catchall"

  Scenario: Kick-back edge on HITL rejection
    Given pipeline "review-pipeline" has a HITL gate at the edge from "author" to "publish"
    And a reject edge exists from "author" back to "fixup"
    When a human rejects the gate
    Then the run routes back to "fixup"
    And the run does not proceed to "publish"

  Scenario: Conditional gate with eval-before-interrupt
    Given pipeline "eval-gate-pipeline" has a HITL gate at the edge from "generator" to "review"
    And the gate has eval definition "quality-check" with threshold 0.7
    When the node "generator" completes with score 0.45
    Then the eval triggers the HITL gate
    And the run transitions to "awaiting_human"

  Scenario: Parallel branches
    Given pipeline "fanout-pipeline" has a splitter node "fanout"
    And "fanout" has parallel edges to "branch-a" and "branch-b"
    When the run reaches "fanout"
    Then both "branch-a" and "branch-b" execute
    And the run completes only after both branches finish

  Scenario: Conditional HITL gate with eval threshold
    Given pipeline "threshold-pipeline" has a HITL gate at the edge from "codegen" to "deploy"
    And the gate has eval definition "security-check" with threshold 0.9 operator "lt"
    When the node "codegen" completes with score 0.95
    Then the eval does not trigger the HITL gate
    And execution continues without interrupting
