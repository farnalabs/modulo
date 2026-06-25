Feature: Marcus — CISO at a Regulated Organisation
  As Marcus, the CISO responsible for security and compliance
  I want every agent action to be auditable, attributable, and reversible
  So that I can approve AI-in-SDLC without expanding our regulatory risk

  @goal-marcus-immutable-audit @awaiting-implementation
  Scenario: Marcus verifies the audit log is append-only
    Given an audit event has been written for a run action
    When I attempt to delete or alter the audit event
    Then the operation is rejected
    And the audit log contains the original unmodified event
    And the audit log is timestamped and attributable

  @goal-marcus-crypto-chain @awaiting-implementation
  Scenario: Marcus confirms the audit log has a cryptographic hash chain
    Given a sequence of 100 audit events
    When I verify the hash chain
    Then each event's hash is derived from the previous event's hash
    And tampering with any event breaks the chain for all subsequent events

  @goal-marcus-data-residency @awaiting-implementation
  Scenario: Marcus confirms no data leaves the organisation's infrastructure
    Given Modulo is deployed in a self-hosted configuration
    When I inspect outbound network connections
    Then no agent output, source code, or credentials leave the VPC
    And no telemetry is sent to external services
    And the only outbound connections are to configured connector endpoints

  @goal-marcus-human-only-gates
  Scenario: Marcus enforces human-only decisions on critical gates
    Given pipeline "deploy-to-prod" has HITL gate "production-deploy"
    When the gate has human_only set to true
    Then only a human user can approve or reject
    And the MCP review_hitl tool returns a "human_only" error
    And an API key with role "runner" cannot approve the gate

  @goal-marcus-credential-isolation
  Scenario: Marcus confirms credentials never leak into state or logs
    Given a run is executing with connector and model backend credentials
    When I inspect the run's LangGraph state
    Then no credential values appear in the state
    When I inspect the run's checkpoint blobs
    Then no credential values appear in checkpoints
    When I inspect the OTel traces
    Then no credential values appear in spans or attributes

  @goal-marcus-credential-encryption
  Scenario: Marcus audits that all stored credentials are encrypted
    Given connector instances and model backends are configured with secrets
    When I inspect the database
    Then all credential fields contain Fernet-encrypted ciphertext
    And decryption occurs only at runtime and is not persisted

  @goal-marcus-tenant-isolation
  Scenario: Marcus confirms tenant isolation under concurrent load
    Given two organisations "acme" and "megacorp" on the same Modulo instance
    When 50 concurrent runs execute across both orgs
    Then no organisation can access another org's pipelines, runs, or credentials
    And RLS is enforced at the database level

  @goal-marcus-offboarding @awaiting-implementation
  Scenario: Marcus confirms offboarding immediately revokes access
    Given user "engineer-bob" has an active JWT session
    When Bob is removed from the organisation
    Then Bob's JWT is invalidated on next API call
    And Bob cannot list pipelines or view runs
    And Bob's refresh tokens are revoked

  @goal-marcus-injection-prevention
  Scenario: Marcus confirms prompt injection is prevented
    Given a pipeline node accepts user-provided input
    When the input contains prompt injection payloads
    Then the input is sanitised before reaching the agent prompt
    And the injection attempt is logged

  @goal-marcus-webhook-integrity
  Scenario: Marcus confirms outbound webhooks are signed and verified
    Given a notification webhook is configured
    When a HITL notification is sent
    Then the webhook payload includes an HMAC-SHA256 signature
    And the receiver can verify the payload integrity

  @goal-marcus-failure-alerting
  Scenario: Marcus is alerted when an agent auth failure occurs
    Given a connector instance has invalid credentials
    When a pipeline attempts to use the connector
    Then a failure webhook is sent to the configured endpoint
    And the connector health status is updated to "unhealthy"
