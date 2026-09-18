Feature: Composite Input/Output Mapping
  As a pipeline author
  I want to define field-level mappings between parent pipeline and composite sub-pipeline
  So that schemas can be adapted when parent and composite schemas differ

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Auto-map compatible schemas — passthrough
    Given a composite template with input schema compatible with the parent pipeline output
    When the pipeline run applies input mapping
    Then no explicit mapping is needed
    And the input is passed through unchanged

  Scenario: Manual field pairing with JMESPath
    Given a composite node with composite_input_mapping = {"report_title": "data.title", "report_body": "data.content"}
    And the parent output contains {"data": {"title": "Q3 Review", "content": "Revenue grew 20%"}}
    When the pipeline run applies input mapping
    Then the sub-pipeline receives {"report_title": "Q3 Review", "report_body": "Revenue grew 20%"}

  Scenario: Partial mapping — some fields mapped, rest omitted
    Given a composite node with composite_input_mapping = {"title": "header.title"}
    And the parent output contains {"header": {"title": "Hello"}, "footer": {"text": "bye"}}
    When the pipeline run applies input mapping
    Then the sub-pipeline receives {"title": "Hello"}
    And it does not contain "footer"

  Scenario: Clear mapping — remove input mapping from composite node
    Given a composite node with an existing composite_input_mapping
    When the user clears the input mapping
    Then the composite node has no input mapping

  Scenario: Clear mapping — remove output mapping from composite node
    Given a composite node with an existing composite_output_mapping
    When the user clears the output mapping
    Then the composite node has no output mapping

  Scenario: Output mapping with JMESPath
    Given a composite node with composite_output_mapping = {"summary": "result.text"}
    And the sub-pipeline produces {"result": {"text": "Analysis complete", "score": 95}}
    When the pipeline run applies output mapping
    Then the parent pipeline receives {"summary": "Analysis complete"}
