Feature: Alice — Head of DevX at an SME
  As Alice, responsible for DevX and engineering productivity at a 30-150 person company
  I want to progressively migrate our SDLC to agentic delivery
  So that we gain speed without losing auditability or human control

  @goal-alice-model-current-sdlc @delivered
  Scenario: Alice models her current SDLC with manual nodes
    Given my team uses: PRD writing (manual), ticket grooming (manual), implementation (manual), deploy (HITL)
    When I create a pipeline with 4 nodes
    And I set the first 3 nodes to type "manual"
    And I set the final node to type "hitl" with human_only true
    Then the pipeline saves successfully
    And the pipeline is executable with no AI agents configured
    And each manual node produces a log entry when completed

  @goal-alice-replace-step @delivered
  Scenario: Alice replaces a manual QA step with an agent
    Given pipeline "current-sdlc" has node "qa-review" set to manual
    When I change node "qa-review" type from "manual" to "agent"
    And I assign schema "qa-report" to the node
    And I bind the GitHub connector for artifact access
    Then the pipeline saves successfully
    And the node "qa-review" now executes as an agent

  @goal-alice-hitl-deploy-gate
  Scenario: Alice enforces human-only approval on deploy
    Given pipeline "current-sdlc" has a HITL gate at "deploy"
    When a run reaches the "deploy" gate
    Then the run pauses
    And the HITL gate has human_only true
    And no MCP tool can approve this gate

  @goal-alice-hitl-proof @delivered
  Scenario: Alice proves HITL compliance to an auditor
    Given pipeline "current-sdlc" has completed 3 runs with HITL approvals
    When I export the audit log for those runs
    Then each run has an immutable audit event for every HITL decision
    And each audit event records: who, when, which gate, and the decision
    And the audit log is append-only

  @goal-alice-rollback-step @delivered
  Scenario: Alice reverts a step replacement when the agent underperforms
    Given node "qa-review" is currently type "agent"
    When I set node "qa-review" back to type "manual"
    Then the pipeline saves successfully
    And I can revert to a previous pipeline snapshot
    When I restore snapshot "pre-qa-agent"
    Then the pipeline matches the state before the agent was added

  @goal-alice-library-start
  Scenario: Alice finds and adapts a library workflow
    Given the community library has a "PRD to tickets" workflow
    When I browse the library
    Then I see the workflow's description, author, and download count
    When I copy the workflow to my workspace
    And I customise the agent prompts for my team's conventions
    Then the forked workflow is saved as a local primitive
    And the forked_from metadata points to the community original

  @goal-alice-team-rbac @delivered
  Scenario: Alice's team owns pipeline config but QA can only view
    Given team "devx" has role "operator"
    And team "qa" has role "viewer"
    When user from "devx" team opens a pipeline for editing
    Then the edit controls are available
    When user from "qa" team opens the same pipeline
    Then the pipeline is visible in read-only mode
    And no edit controls are shown

  @goal-alice-soc2-evidence @delivered
  Scenario: Alice's SOC 2 auditor reviews HITL evidence
    Given a completed run with 2 HITL approvals and 1 HITL rejection
    When I navigate to the run detail
    Then I see each HITL event with timestamp and reviewer identity
    And I see the rejection reason
    And the run audit trail is exportable as a PDF or CSV

  @goal-alice-connector-swap
  Scenario: Alice swaps from GitHub to GitLab without pipeline changes
    Given pipeline "current-sdlc" has a node bound to connector "github"
    When I create a new connector instance of type "git-host" for GitLab
    And I update the node's connector binding to "gitlab"
    Then the pipeline saves successfully
    And the node reads from GitLab on the next run

  @goal-alice-incremental-trust @delivered
  Scenario: Alice adds automated evals before increasing agent autonomy
    Given pipeline "current-sdlc" has agent node "ticket-writer"
    When I add an eval suite to the node with pass_threshold 0.8
    And the eval suite runs on each agent output
    Then failed evals block the pipeline
    And the eval pass rate trend is visible on the pipeline dashboard

  @goal-alice-hitl-webhook
  Scenario: Alice's team gets Slack notifications when HITL is waiting
    Given a run is waiting at HITL gate "deploy"
    When the HITL gate triggers a notification
    Then a webhook POST is sent to the configured Slack endpoint
    And the webhook payload includes the run ID and gate name
