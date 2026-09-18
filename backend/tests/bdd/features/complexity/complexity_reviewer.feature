Feature: Complexity Reviewer
  As a pipeline author
  I want the system to assess SDLC complexity from connector data
  So that I can generate pipeline drafts and automation suggestions

  Scenario: GitHub connector scan produces repo and PR samples
    Given a GitHub connector with sample data
    When the determination scanner samples the connector
    Then the samples include repos
    And the samples include pull requests

  Scenario: Jira connector scan produces issue samples
    Given a Jira connector with sample data
    When the determination scanner samples the connector
    Then the samples include issues

  Scenario: Pipeline draft is generated from findings
    Given scanned samples with planning and development stages
    When a pipeline draft is generated
    Then the draft has a start node and an end node
    And the draft has at least one agent node

  Scenario: Empty scan produces no draft
    Given no connectors have data
    When a pipeline draft is generated
    Then the draft has no nodes

  Scenario: Missing model backend does not block draft generation
    Given scanned samples with planning stage
    When a pipeline draft is generated
    Then the draft contains automation suggestions
