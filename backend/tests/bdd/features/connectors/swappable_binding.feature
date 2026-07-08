Feature: Swappable Connector Binding
  As a pipeline operator
  I want to swap a connector binding without modifying the agent configuration
  So that the same pipeline can run against different data sources

  Background:
    Given I am authenticated in org "acme"

  Scenario: Swap filesystem connector for GitHub connector
    Given a pipeline with a node that uses connector type "filesystem"
    And a filesystem connector "local-fs" exists
    And a GitHub connector "github-prod" exists
    When I bind the node to connector "github-prod"
    Then the node uses connector "github-prod"
    And the node no longer uses "local-fs"

  Scenario: Binding persists across pipeline save
    Given a pipeline with a connector binding to "local-fs"
    When I save the pipeline with binding to "github-prod"
    And I load the pipeline again
    Then the connector binding is "github-prod"

  Scenario: Binding validation rejects incompatible types
    Given a pipeline with a node that requires connector type "filesystem"
    When I try to bind the node to a GitHub connector
    Then the binding is rejected with a type mismatch error

  Scenario: Default binding is used when no override exists
    Given a pipeline with a node that uses connector type "filesystem"
    And a default connector "default-fs" of type "filesystem"
    When I inspect the node binding
    Then the node uses the default connector "default-fs"
