Feature: Composite Node Runtime
  As a pipeline runner
  I want to expand composite nodes at runtime and inject parameter values
  So that sub-pipelines execute with correct configuration

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Expand a composite node into its sub-pipeline nodes
    Given a composite template "review-composite" with sub-pipeline containing 3 nodes
    And the pipeline graph has a composite node referencing template "review-composite"
    When the pipeline run expands the composite node
    Then 3 expanded nodes are produced
    And each expanded node has the composite parent id set
    And each expanded node has a unique composite index

  Scenario: Inject parameter values into sub-pipeline nodes via prompt_replace
    Given a composite template with parameter "tone" injected into the agent prompt
    And the pipeline run provides parameter value tone="critical"
    When the pipeline run expands the composite node
    Then the expanded node prompt contains "critical" instead of "{{parameter.tone}}"

  Scenario: Apply input schema mapping — passthrough when compatible
    Given a composite node with compatible input schema
    When the pipeline run applies input mapping
    Then the input is passed through unchanged

  Scenario: Apply field mapping via JMESPath expressions
    Given a composite node with composite_input_mapping mapping "title" to "input.title"
    When the pipeline run processes the composite node
    Then the sub-pipeline receives the mapped data with "title" key

  Scenario: Graph validator rejects missing required parameter values
    Given a composite template with a required parameter "api_key" and no default
    And the pipeline graph has a composite node without providing "api_key"
    When the graph validator checks the pipeline
    Then the validator returns an error "Missing required parameter: api_key"

  Scenario: Graph validator rejects composite with non-existent template_id
    Given a pipeline graph has a composite node referencing a non-existent template id "00000000-0000-0000-0000-000000099999"
    When the graph validator checks the pipeline
    Then the validator returns an error "Composite template not found"

  Scenario: Composite bindings captured in PipelineSnapshot
    Given a pipeline with a composite node referencing template "review-composite" version "1.0.0"
    When a snapshot is created for the pipeline
    Then the snapshot contains composite bindings
    And the bindings include composite_template_id and composite_version

  Scenario: Expand composite with no sub-pipeline nodes returns error
    Given a composite template "empty-composite" with no sub-pipeline nodes
    And the pipeline graph has a composite node referencing template "empty-composite"
    When the pipeline run expands the composite node
    Then an error is returned "no sub-pipeline nodes"

  Scenario: Composite node without composite_ref is rejected
    Given a pipeline graph has a composite node without composite_ref
    When the graph validator checks the pipeline
    Then the validator returns an error "composite_ref"
