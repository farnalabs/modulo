Feature: Model Backend Selection
  As a pipeline author
  I want each node to select its model backend explicitly
  So that different stages of a pipeline can use different AI models

  Scenario: Node uses configured backend override
    Given a pipeline with a per-node backend override
    When node "code-review" executes
    Then the backend for node "code-review" is "anthropic/claude-3-opus"

  Scenario: Default backend used when no override exists
    Given an org with a default backend "openai/gpt-4o-mini" configured
    When a node without an override executes
    Then the default backend is used for nodes without an override

  Scenario: Fallback chain activates on primary failure
    Given a pipeline with backend fallback chain configured
    When the primary backend is unhealthy
    Then the fallback backend "openai/gpt-4o" is selected

  Scenario: Unknown backend returns error
    Given a pipeline references an unknown backend "nonexistent/model"
    When the pipeline attempts to resolve backends
    Then a backend resolution error is raised
