Feature: Guardrail Detection Engine
  As a pipeline author
  I want guardrails to enforce boundary constraints at the ingestion edge
  So that unsafe payloads are blocked, redacted, or observed before a run is created

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Block-action guardrail raises GuardrailBlockedError when regex detects a violation
    Given a guardrail "no-secrets" with block action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    When the guardrail engine evaluates the payload {"body": "leak SECRET_ABC12345"}
    Then a GuardrailBlockedError is raised for guardrail "no-secrets"

  Scenario: Block-action guardrail passes a clean payload
    Given a guardrail "no-secrets" with block action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    When the guardrail engine evaluates the payload {"body": "clean text"}
    Then no GuardrailBlockedError is raised

  Scenario: Warn-action guardrail never raises on a violation
    Given a guardrail "no-secrets" with warn action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    When the guardrail engine evaluates the payload {"body": "leak SECRET_ABC12345"}
    Then no GuardrailBlockedError is raised

  Scenario: Redact-action guardrail masks a sensitive field with a fixed mask
    Given a guardrail "redact-key" with redact action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    And the guardrail has a transform redaction policy on path "credentials.api_key"
    When the interception pass runs over the payload {"credentials": {"api_key": "sk-live-123"}, "body": "clean"}
    Then the persisted payload masks "credentials.api_key"
    And the original payload is not mutated

  Scenario: Interception pass records a block without raising
    Given a guardrail "no-secrets" with block action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    When the interception pass runs over the payload {"body": "leak SECRET_ABC12345"}
    Then the interception outcome reports blocked by "no-secrets"

  Scenario: A guardrail routed through the generic engine raises GuardrailMisroutedError
    Given a guardrail "never-here" with block action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    When the generic eval engine evaluates the guardrail directly
    Then a GuardrailMisroutedError is raised

  Scenario: Guardrail with retry failure behaviour is rejected at validation
    Given a guardrail "no-retry" with block action
    And the guardrail detects regex pattern "SECRET_[A-Z0-9]{8}" on field "body"
    When the guardrail is forced to carry failure_behaviour "retry"
    Then a GuardrailConfigError is raised

  Scenario: Conformance derivation is present when all required capabilities are confirmed
    Given a guardrail "github-write" with block action requiring capability "github:write"
    And the registered capability "github:write" is confirmed present
    When conformance state is derived
    Then the conformance state is "present"

  Scenario: Conformance derivation is absent when a required capability is confirmed missing
    Given a guardrail "github-write" with block action requiring capability "github:write"
    And the registered capability "github:write" is confirmed absent
    When conformance state is derived
    Then the conformance state is "absent"
