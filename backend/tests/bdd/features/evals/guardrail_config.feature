Feature: Guardrail Config-as-Code Workflow
  As a pipeline operator
  I want to manage guardrails as code through a propose-apply-reject workflow
  So that guardrail changes are reviewed, hashed, and drift is detectable

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Proposing a valid config set stores a proposal with a diff
    Given there is no applied guardrail config
    When I propose the guardrail config:
      """
      version: 1
      guardrails:
        - id: no-secrets
          name: No Secrets
          action: block
          detection:
            type: regex
            pattern: "SECRET_[A-Z0-9]{8}"
            field: body
      """
    Then the response status is 200
    And the proposal is accepted
    And the proposal hash is a 64-character hex digest
    And the diff lists an "add" for guardrail "no-secrets"

  Scenario: Proposing a config that updates an existing guardrail lists an update
    Given a guardrail "no-secrets" was previously applied
    When I propose the guardrail config:
      """
      version: 1
      guardrails:
        - id: no-secrets
          name: No Secrets
          action: warn
          detection:
            type: regex
            pattern: "SECRET_[A-Z0-9]{8}"
            field: body
      """
    Then the response status is 200
    And the diff lists an "update" for guardrail "no-secrets"

  Scenario: Proposing malformed YAML is rejected with 422
    When I propose the guardrail config "not: [valid: yaml"
    Then the response status is 422

  Scenario: Proposing a config with a rule violation is rejected with 422
    When I propose the guardrail config:
      """
      version: 1
      guardrails:
        - id: no-secrets
          name: No Secrets
          action: block
          detection:
            type: regex
            field: body
      """
    Then the response status is 422

  Scenario: Applying with no pending proposal returns 409
    When I apply the guardrail config
    Then the response status is 409

  Scenario: Applying a pending proposal reconciles the guardrail rows
    Given a pending guardrail proposal exists
    When I apply the guardrail config
    Then the response status is 200
    And the apply reports a clean applied state
    And the guardrail rows were reconciled

  Scenario: Rejecting a pending proposal discards it
    Given a pending guardrail proposal exists
    When I reject the guardrail config
    Then the response status is 200
    And the reject reports a clean state

  Scenario: Rejecting with no pending proposal returns 409
    When I reject the guardrail config
    Then the response status is 409

  Scenario: Drift reports clean when live rows match the applied pin
    Given a guardrail config was previously applied
    And the live guardrail rows match the applied pin
    When I request the guardrail config drift
    Then the response status is 200
    And the drift response reports "clean"

  Scenario: Drift reports drift when live rows diverge from the applied pin
    Given a guardrail config was previously applied
    And the live guardrail rows diverge from the applied pin
    When I request the guardrail config drift
    Then the response status is 200
    And the drift response reports "drift"

  Scenario: A viewer cannot propose guardrail config
    Given I am authenticated as a viewer in org "acme"
    When I propose the guardrail config:
      """
      version: 1
      guardrails:
        - id: no-secrets
          name: No Secrets
          action: block
          detection:
            type: regex
            pattern: "SECRET_[A-Z0-9]{8}"
            field: body
      """
    Then the response status is 403

  Scenario: The standard read masks the deny-rule internals but the elevated read shows them
    Given a guardrail config with a regex pattern was applied
    When I read the guardrail config as an operator
    Then the response status is 200
    And the response config masks the regex pattern
    When I read the elevated guardrail config as an admin
    Then the response status is 200
    And the response config shows the real regex pattern

  Scenario: A non-admin cannot read the elevated guardrail config
    Given a guardrail config with a regex pattern was applied
    When I read the elevated guardrail config as an operator
    Then the response status is 403
