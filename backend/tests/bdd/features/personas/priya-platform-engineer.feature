Feature: Priya — Platform Engineer at a Scaling Org
  As Priya, a platform engineer evaluating and deploying governed agentic delivery
  I want to roll out agentic SDLC team-by-team with central policy
  So that we capture AI's speed without expanding risk

  @goal-priya-self-hosted-k8s @delivered
  Scenario: Priya deploys Modulo on existing Kubernetes infrastructure
    Given I have a Kubernetes cluster with Postgres and Redis
    When I deploy the Modulo Helm chart
    Then the application starts and runs health checks
    And all data stays within the cluster's VPC

  @goal-priya-sso-okta @delivered
  Scenario: Priya integrates Okta SSO with JIT provisioning
    Given my org uses Okta for identity
    When I configure the OIDC provider with my Okta tenant
    Then users authenticate via Okta SSO
    And new users are JIT-provisioned with org role "viewer"

  @goal-priya-team-isolation @delivered
  Scenario: Priya isolates teams so they only see their own pipelines
    Given org "acme" has teams "alpha" and "beta"
    And team "alpha" owns pipeline "payment-workflow"
    When user from "beta" views the pipeline list
    Then pipeline "payment-workflow" is not visible
    When a user with admin role views the pipeline list
    Then all pipelines are visible

  @goal-priya-api-key-ci
  Scenario: Priya's CI pipeline triggers runs via runner API key
    Given a CI job needs to trigger a Modulo run
    When I create an API key with role "runner"
    And the CI job uses the key to POST /api/runs
    Then the run is created with status "pending"
    And the run is attributed to the API key, not a user

  @goal-priya-concurrency-control
  Scenario: Priya enforces org-wide max concurrent runs
    Given org "acme" has max_concurrent_runs set to 5
    When 5 runs are already active
    And a 6th run is triggered
    Then the 6th run is rejected with a concurrency limit error

  @goal-priya-ab-test-models @delivered
  Scenario: Priya A/B tests Claude Sonnet vs GPT-4o on the same pipeline
    Given pipeline "code-review" has two variant groups
    When variant A routes to Claude Sonnet
    And variant B routes to GPT-4o
    And both variants run against the same input
    Then each variant produces a result with eval scores
    And I can compare eval scores side-by-side

  @goal-priya-eval-gate @delivered
  Scenario: Priya enforces minimum eval thresholds per team
    Given team "alpha" has eval suite with pass_threshold 0.85
    When a pipeline run completes with eval score 0.72
    Then the run is marked "failed"
    And the output is not promoted to the next stage

  @goal-priya-central-credentials
  Scenario: Priya manages model backends centrally with encrypted credentials
    Given I configure model backend "anthropic-claude" with API key
    When I save the configuration
    Then the API key is Fernet-encrypted at rest
    And the plaintext key never appears in logs, state, or traces
    And only admins can view or edit the backend configuration

  @goal-priya-auto-failover @delivered
  Scenario: Priya's pipelines fail over when a model provider has an outage
    Given model backend "openai-gpt4" health check returns unhealthy
    And pipeline "ticket-writer" is configured with fallback backend "claude-sonnet"
    When a run starts
    Then the unhealthy backend is skipped
    And the fallback backend is used for the run

  @goal-priya-org-dashboard @delivered
  Scenario: Priya sees org-wide adoption metrics
    Given 3 teams are using Modulo with active pipelines
    When I navigate to the organisation dashboard
    Then I see total runs, active pipelines, and avg eval pass rate
    And I see token spend broken down by team

  @goal-priya-feedback-loop @delivered
  Scenario: Priya's HITL rejections grow the eval suite automatically
    Given a HITL rejection on node "ticket-writer" with reason "missing edge case"
    When the rejection is recorded
    Then a FeedbackRecord is created
    And the eval suite for "ticket-writer" is proposed for expansion
    And I can review and approve the new eval case
