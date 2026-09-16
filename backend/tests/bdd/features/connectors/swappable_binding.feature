Feature: Swappable Connector Binding
  As a pipeline operator
  I want to swap a node's connector binding without modifying the rest of the graph
  So that the same pipeline can run against different data sources

  Scenario: Swap a node's connector binding without touching the graph topology
    Given a pipeline graph with a node "analyze" bound to connector instance "local-fs"
    When the node "analyze" binding is swapped to connector instance "github-prod"
    Then the graph carries exactly one connector binding pointing at "github-prod"
    And no connector binding references "local-fs"

  Scenario: A node without a binding extracts no connector binding
    Given a pipeline graph with a node "analyze" and no connector binding
    When I extract the graph's connector bindings
    Then no connector binding is extracted

  Scenario: A binding to a missing connector instance is rejected
    Given a pipeline graph with a node "analyze" bound to connector instance "missing-conn"
    When I validate the graph with its connector bindings
    Then the graph is rejected with error code "CONNECTOR_NOT_FOUND"

  Scenario: A binding whose connector lacks a required operation is rejected
    Given a pipeline graph with a node "analyze" bound to connector instance "github-prod"
    And connector instance "github-prod" allows only "read"
    When I validate the graph with required operations "write"
    Then the graph is rejected with error code "CONNECTOR_MISSING_OPERATIONS"

  Scenario: A valid active binding is accepted
    Given a pipeline graph with a node "analyze" bound to connector instance "github-prod"
    And connector instance "github-prod" allows "read" and "write"
    When I validate the graph with required operations "read"
    Then the graph is valid