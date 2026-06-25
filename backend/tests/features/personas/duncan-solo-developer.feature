Feature: Duncan — Solo Developer
  As Duncan, a solo developer shipping and operating a SaaS product
  I want Modulo to run my agentic SDLC autonomously
  So that I spend my time on code, not process

  @goal-solo-self-hosted @awaiting-implementation
  Scenario: Duncan self-hosts with docker compose up
    Given I have Docker installed
    When I run docker compose up
    Then the Modulo application starts
    And I can access the UI at http://localhost:8000

  @goal-solo-first-pipeline @awaiting-implementation
  Scenario: Duncan triggers a run from a library workflow
    Given the library contains a "PRD to tickets" workflow
    When I copy the workflow to my workspace
    And I configure my GitHub connector
    And I trigger a manual run
    Then the run starts with status "pending"
    And the run completes successfully
    And tickets are created in my issue tracker

  @goal-solo-model-rotation
  Scenario: Duncan rotates between AI providers per node
    Given pipeline "release-pipeline" has 3 nodes
    When node "planner" is bound to model backend "claude-sonnet"
    And node "coder" is bound to model backend "gpt-4o"
    And node "reviewer" is bound to model backend "ollama-llama3"
    And I trigger a run
    Then node "planner" executes against Claude
    And node "coder" executes against GPT-4o
    And node "reviewer" executes against Ollama

  @goal-solo-model-fallback @awaiting-implementation
  Scenario: Duncan's pipeline fails over when a model backend is unhealthy
    Given model backend "claude-sonnet" is unhealthy
    And pipeline "release-pipeline" has node "planner" bound to "claude-sonnet"
    When I trigger a run
    Then the run status becomes "failed"
    And the error_detail describes the backend health check failure

  @goal-solo-checkpoint-resume
  Scenario: Duncan resumes a failed run from its last checkpoint
    Given a run for pipeline "release-pipeline" failed at node "reviewer"
    When I resume the run
    Then the run restarts from node "reviewer"
    And earlier node outputs are preserved

  @goal-solo-portable
  Scenario: Duncan exports and re-imports his pipeline on another machine
    Given I have a configured pipeline "release-pipeline"
    When I export the pipeline as a YAML bundle
    Then the bundle contains no credentials
    And the bundle references abstract schema names
    When I import the bundle on another Modulo instance
    Then a new pipeline is created with the same node topology

  @goal-solo-eval-gate @awaiting-implementation
  Scenario: Duncan's deploy gate requires passing evals
    Given pipeline "release-pipeline" has an eval suite with pass_threshold 0.9
    And a completed run scored 0.85
    When the eval engine finishes
    Then the run status is "failed"
    And the deploy does not proceed

  @goal-solo-single-hitl
  Scenario: Duncan approves only the deploy gate manually
    Given a run is waiting at HITL gate "deploy"
    When I claim the gate
    And I approve the gate with my decision
    Then the run resumes
    And the audit log records my approval

  @goal-solo-observability @awaiting-implementation
  Scenario: Duncan inspects a run trace to understand token spend
    Given a completed run for pipeline "release-pipeline"
    When I view the run detail
    Then I see per-node token consumption
    And I see the total run cost
    And I see the OTel trace ID

  @goal-solo-grow-complexity @awaiting-implementation
  Scenario: Duncan adds a node to his pipeline without breaking existing runs
    Given pipeline "release-pipeline" has 3 nodes
    When I add a new "release-notes" node between "coder" and "reviewer"
    Then the pipeline saves successfully
    And existing runs against the previous snapshot are unaffected
