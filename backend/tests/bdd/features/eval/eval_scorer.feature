Feature: Eval Scorer Dispatch
  As a pipeline author
  I want the eval engine to apply the correct scorer per criterion
  So that different eval types (regex, LLM judge, JSON Schema, custom) work correctly

  Scenario: Regex scorer matches expected pattern
    Given an eval suite with multiple scorer types
    And the criterion uses eval_type "regex" with pattern "success"
    When the eval engine scores using each scorer
    Then the correct scorer is applied per criterion
    And the output "mission success" passes the regex scorer

  Scenario: Regex scorer fails on non-matching output
    Given an eval suite with multiple scorer types
    And the criterion uses eval_type "regex" with pattern "error"
    When the eval engine scores using each scorer
    Then the output "mission success" fails the regex scorer

  Scenario: JSON Schema scorer validates structure
    Given an eval suite with multiple scorer types
    And the criterion uses eval_type "json_schema" with a schema
    When the eval engine scores using each scorer
    Then the correct scorer is applied per criterion
    And valid data passes the json_schema scorer

  Scenario: Custom function scorer executes correctly
    Given an eval suite with multiple scorer types
    And the criterion uses eval_type "custom_function"
    When the eval engine scores using each scorer
    Then the correct scorer is applied per criterion

  Scenario: Unknown eval type raises an error
    Given an eval suite with multiple scorer types
    And the criterion uses eval_type "unknown_type"
    When the eval engine scores using each scorer
    Then an error is raised for unknown eval type
