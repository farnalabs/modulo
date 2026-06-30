Feature: Dogfooding Pipeline
  The dogfooding pipeline is a pre-built canonical workflow where Modulo
  builds Modulo — reading a GitHub issue, generating a code diff via LLM,
  validating with tests, passing through a human review gate, and creating a PR.

  Background:
    Given the dogfooding pipeline definition exists in the library

  Scenario: Dogfooding pipeline has the correct name
    When the dogfooding pipeline definition is inspected
    Then its name is "Dogfooding Pipeline"
    And its version is "1.0.0"
    And its author is "Modulo"

  Scenario: Dogfooding pipeline has the expected five steps
    When the dogfooding pipeline steps are inspected
    Then there are 5 steps
    And the steps are in order: "read-issue", "generate-diff", "validate", "review-gate", "create-pr"

  Scenario: Dogfooding pipeline read-issue step
    When step "read-issue" is inspected
    Then it has no agent
    And it has a connector binding of type "source_control" that is required

  Scenario: Dogfooding pipeline generate-diff step
    When step "generate-diff" is inspected
    Then it uses agent "correction-proposer"
    And it depends on "read-issue"

  Scenario: Dogfooding pipeline validate step
    When step "validate" is inspected
    Then it uses agent "test-generator"
    And it depends on "generate-diff"
    And it has a connector binding of type "ci_runner" that is optional

  Scenario: Dogfooding pipeline review-gate step
    When step "review-gate" is inspected
    Then it uses agent "code-reviewer"
    And it depends on "validate"

  Scenario: Dogfooding pipeline create-pr step
    When step "create-pr" is inspected
    Then it has no agent
    And it depends on "review-gate"
    And it has a connector binding of type "source_control" that is required

  Scenario: Dogfooding pipeline default config has all required keys
    When the dogfooding pipeline default config is inspected
    Then it contains all expected default config keys

  Scenario: All dogfooding pipeline agent references are known agents
    When all agent references in the dogfooding pipeline are checked
    Then every referenced agent exists in the known agents set

  Scenario: All dogfooding pipeline connector bindings are valid types
    When all connector bindings in the dogfooding pipeline are checked
    Then every connector type is a valid known type

  Scenario: Dogfooding pipeline dependency chain has no cycles or orphans
    When the dependency chain of the dogfooding pipeline is validated
    Then every dependency reference points to an existing step
    And there are no circular dependencies

  Scenario: Dogfooding pipeline tags include canonical
    When the dogfooding pipeline tags are inspected
    Then the tags include "canonical"
    And the tags include "dogfooding"
    And the tags include "issue-to-pr"

  Scenario: Dogfooding pipeline can be serialised to JSON and back
    When the dogfooding pipeline definition is serialised to JSON
    Then it can be deserialised without data loss
    And the deserialised name is "Dogfooding Pipeline"
