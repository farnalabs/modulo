Feature: Regex Eval
  As a pipeline author
  I want to validate agent output against a regex pattern
  So that I can enforce structural or content constraints

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Regex pattern matches output field
    Given node "test-writer" has a regex eval "has-assertions"
    And the eval config has pattern "assert .+"
    And the eval config has field "code"
    When the node outputs {"code": "def test_foo(): assert result == 42"}
    Then the eval result has passed true
    And the eval result has score 1.0

  Scenario: Regex pattern does not match output field
    Given node "test-writer" has a regex eval "has-assertions"
    And the eval config has pattern "assert .+"
    And the eval config has field "code"
    When the node outputs {"code": "def test_foo(): pass"}
    Then the eval result has passed false
    And the eval result has score 0.0

  Scenario: Regex eval on a nested field
    Given node "doc-generator" has a regex eval "contains-title"
    And the eval config has pattern "## .+"
    And the eval config has field "content"
    When the node outputs {"content": "## Overview\nThis is a document"}
    Then the eval result has passed true
    And the eval result has score 1.0

  Scenario: Regex eval with block behaviour on no match
    Given node "formatter" has a regex eval "no-todos"
    And the eval config has pattern "TODO|FIXME"
    And the eval config has field "code"
    And the eval has failure_behaviour "block"
    When the node outputs {"code": "# TODO: implement this function"}
    Then the eval result has passed true
    And an EvalBlockedError is raised

  Scenario: Regex eval with warn behaviour on no match
    Given node "formatter" has a regex eval "no-todos"
    And the eval config has pattern "TODO|FIXME"
    And the eval config has field "code"
    And the eval has failure_behaviour "warn"
    When the node outputs {"code": "# TODO: implement this function"}
    Then the eval result has passed true
    And a warning is logged
    And pipeline execution continues

  Scenario: Regex eval with missing config returns failed
    Given node "test-writer" has a regex eval "bad-config"
    When the node outputs {"code": "def test_foo(): pass"}
    And the eval config is missing "pattern"
    Then the eval result has passed false
    And the eval result has score 0.0

  Scenario: Regex pattern matches anywhere in the field value
    Given node "doc-generator" has a regex eval "contains-error"
    And the eval config has pattern "error|fail"
    And the eval config has field "summary"
    When the node outputs {"summary": "Pipeline completed with zero errors"}
    Then the eval result has passed true

  Scenario: Regex eval field is coerced to string
    Given node "data-processor" has a regex eval "numeric-output"
    And the eval config has pattern "^\d+$"
    And the eval config has field "count"
    When the node outputs {"count": 42}
    Then the eval result has passed true
