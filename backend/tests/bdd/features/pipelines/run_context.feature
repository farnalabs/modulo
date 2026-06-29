Feature: Run Context — Seeding, Write Guard, and Audit
  As a pipeline operator
  I want run_context to be seeded at run start, writable only by designated
  context-setter nodes, and audited on violations
  So that runtime state is both flexible and secure

  Scenario: Context-setter writes to run_context
    Given pipeline "deploy-service" with a context_setter node "reviewer"
    And I am authenticated in org "acme"
    When the context_setter node "reviewer" writes "model_tier"="tier-2" to run_context
    Then the write is accepted
    And run_context contains "model_tier"="tier-2"
    And the write-log has 1 entry for node "reviewer"

  Scenario: Non-setter write is rejected
    Given pipeline "deploy-service" with an agent node "coder"
    And I am authenticated in org "acme"
    When the agent node "coder" attempts to write "secret"="data" to run_context
    Then the write is rejected with ContextSetterViolationError

  Scenario: Context seeded at run start
    Given pipeline "deploy-service" has run_context_defaults
    And the defaults include "env" = "prod"
    And the defaults include "branch" = "main"
    And a run is triggered with input_payload "task" = "deploy"
    When the run starts
    Then the seeded run_context contains "env" = "prod"
    And the seeded run_context contains "branch" = "main"
    And the seeded run_context input has "task" = "deploy"
    And seeded run_context has cancelled = "false"

  Scenario: Last-write-wins semantics
    Given pipeline "deploy-service" with a context_setter node "tier-setter"
    And I am authenticated in org "acme"
    When the context_setter node "tier-setter" writes "model_tier"="tier-1" to run_context
    And the context_setter node "tier-setter" writes "model_tier"="tier-3" to run_context
    Then run_context contains "model_tier"="tier-3"
    And the write-log has 2 entry for node "tier-setter"

  Scenario: Multiple context-setters append to write log
    Given pipeline "deploy-service" with context_setter nodes "setter-a" and "setter-b"
    And I am authenticated in org "acme"
    When the context_setter node "setter-a" writes "field_a"="value_a" to run_context
    And the context_setter node "setter-b" writes "field_b"="value_b" to run_context
    Then the write-log has 2 entries
    And write-log entry 0 node_name is "setter-a"
    And write-log entry 1 node_name is "setter-b"
    And run_context contains "field_a"="value_a"
    And run_context contains "field_b"="value_b"

  Scenario: Context accessible by all nodes
    Given pipeline "deploy-service" with agent nodes "reader-a" and "reader-b"
    And run_context contains "branch" = "main"
    When each agent reads the run_context field "branch"
    Then each agent sees "branch" = "main"

  Scenario: Artifact vs context separation
    Given a running pipeline "deploy-service" with initial state
    When a node writes artifact "result"="done" to the state
    Then the state has top-level keys "run_context" and "artifacts"
    And "run_context" is not nested inside artifacts
    And "artifacts" is not nested inside run_context

  Scenario: Audit warning on guard violation
    Given pipeline "deploy-service" with an agent node "bad-actor"
    When the agent node "bad-actor" attempts to write to run_context
    Then a warning is logged containing "run_context.violation"
    And the warning includes the node name "bad-actor"
    And the warning includes the attempted fields
