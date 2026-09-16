Feature: Pipeline Graph Validation
  As a pipeline author
  I want invalid pipeline graphs to be rejected with clear errors at save time
  So that a broken pipeline cannot be saved

  Scenario: Reject a graph with no nodes
    Given a pipeline graph with no nodes
    When I validate the pipeline graph
    Then the graph is rejected with error code "TOPOLOGY_NO_NODES"

  Scenario: Reject a graph that omits the nodes field
    Given a pipeline graph definition without a nodes field
    When I validate the pipeline graph
    Then the graph is rejected with error code "TOPOLOGY_NO_NODES"

  Scenario: Reject a circular dependency between nodes
    Given a pipeline graph where node "a" feeds node "b" and node "b" feeds node "a"
    When I validate the pipeline graph
    Then the graph is rejected with error code "TOPOLOGY_CYCLE"

  Scenario: Reject an edge that references an unknown node
    Given a pipeline graph with node "a" and an edge targeting unknown node "ghost"
    When I validate the pipeline graph
    Then the graph is rejected with error code "TOPOLOGY_UNKNOWN_TARGET"

  Scenario: Accept a valid minimal pipeline graph
    Given a valid minimal pipeline graph with one node
    When I validate the pipeline graph
    Then the graph is valid
