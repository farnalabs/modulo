Feature: GitHub Issues Connector
  As a pipeline author
  I want to interact with GitHub issues, labels, milestones, and comments
  So that my agents can manage project tracking and collaboration

  Background:
    Given I am authenticated in org "acme"

  Scenario: Connector lists issues
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector lists issues
    Then the result contains open issues

  Scenario: Connector fetches a single issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector fetches issue number 42
    Then the connector returns the issue details

  Scenario: Connector lists labels
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector lists labels
    Then the result contains label metadata

  Scenario: Connector lists milestones
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector lists milestones
    Then the result contains milestone metadata

  Scenario: Connector lists issue comments
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector lists comments on issue 42
    Then the result contains comment metadata

  Scenario: Connector lists issue events
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector lists events on issue 42
    Then the result contains event metadata

  Scenario: Connector lists assignees
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector lists assignees
    Then the result contains assignee metadata

  Scenario: Connector fetches issue timeline
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector fetches timeline for issue 42
    Then the result contains timeline events

  Scenario: Connector creates an issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector creates an issue with title "Bug found" and body "Details here"
    Then the issue is created successfully

  Scenario: Connector updates an issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector updates issue 42 with state "closed"
    Then the issue is updated successfully

  Scenario: Connector comments on an issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector comments on issue 42 with "Fixed this"
    Then the comment is posted successfully

  Scenario: Connector adds labels to an issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector adds labels "bug,urgent" to issue 42
    Then the labels are added successfully

  Scenario: Connector reacts to an issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    And an issue exists with number 42
    When the connector adds a reaction "+1" to issue 42
    Then the reaction is posted successfully

  Scenario: Connector creates a label
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector creates a label "bug" with color "ff0000"
    Then the label is created successfully

  Scenario: Connector creates a milestone
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector creates a milestone "v1.0" with description "First release"
    Then the milestone is created successfully
