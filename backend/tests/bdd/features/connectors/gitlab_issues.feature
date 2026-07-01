Feature: GitLab Issues Connector
  As a pipeline author
  I want to interact with GitLab issues, labels, milestones, notes, and CI via the connector
  So that I can manage project tracking and pipelines

  Scenario: Query issues returns list
    Given a GitLab connector with valid token
    When I query GitLab resource "issues" with project "group/project" and state "opened"
    Then the result has records
    And the records contain issue metadata

  Scenario: Query single issue by IID
    Given a GitLab connector with valid token
    When I query GitLab resource "issue" with project "group/project" and iid "42"
    Then the result has records
    And the record contains issue fields

  Scenario: Query labels returns list
    Given a GitLab connector with valid token
    When I query GitLab resource "labels" with project "group/project"
    Then the result has records

  Scenario: Query single label by ID
    Given a GitLab connector with valid token
    When I query GitLab resource "label" with project "group/project" and label_id "1"
    Then the result has records

  Scenario: Query milestones returns list
    Given a GitLab connector with valid token
    When I query GitLab resource "milestones" with project "group/project"
    Then the result has records

  Scenario: Query issue notes
    Given a GitLab connector with valid token
    When I query GitLab resource "issue_notes" with project "group/project" and iid "42"
    Then the result has records

  Scenario: Query issue discussions
    Given a GitLab connector with valid token
    When I query GitLab resource "issue_discussions" with project "group/project" and iid "42"
    Then the result has records

  Scenario: Query merge requests with filters
    Given a GitLab connector with valid token
    When I query GitLab resource "merge_requests" with project "group/project" and state "opened"
    Then the result has records

  Scenario: Query single merge request by IID
    Given a GitLab connector with valid token
    When I query GitLab resource "merge_request" with project "group/project" and iid "5"
    Then the result has records

  Scenario: Query branch by name
    Given a GitLab connector with valid token
    When I query GitLab resource "branch" with project "group/project" and name "main"
    Then the result has records

  Scenario: Query branches list
    Given a GitLab connector with valid token
    When I query GitLab resource "branches" with project "group/project"
    Then the result has records

  Scenario: Query tags list
    Given a GitLab connector with valid token
    When I query GitLab resource "tags" with project "group/project"
    Then the result has records

  Scenario: Query pipelines list
    Given a GitLab connector with valid token
    When I query GitLab resource "pipelines" with project "group/project"
    Then the result has records

  Scenario: Query jobs for a pipeline
    Given a GitLab connector with valid token
    When I query GitLab resource "jobs" with project "group/project" and pipeline_id "1"
    Then the result has records

  Scenario: Write creates an issue
    Given a GitLab connector with valid token
    When I write GitLab issue with project "group/project" and title "Test Issue"
    Then the write succeeds

  Scenario: Write updates an issue
    Given a GitLab connector with valid token
    When I write GitLab issue_update for issue "42" with project "group/project" and state_event "close"
    Then the write succeeds

  Scenario: Write adds an issue note
    Given a GitLab connector with valid token
    When I write GitLab issue_note for issue "42" with project "group/project" and body "Fixed this"
    Then the write succeeds

  Scenario: Write replaces issue labels
    Given a GitLab connector with valid token
    When I write GitLab issue_label for issue "42" with project "group/project" and labels "bug,frontend"
    Then the write succeeds

  Scenario: Write creates a project label
    Given a GitLab connector with valid token
    When I write GitLab label with project "group/project" and name "bug"
    Then the write succeeds

  Scenario: Write creates a milestone
    Given a GitLab connector with valid token
    When I write GitLab milestone with project "group/project" and title "Sprint 1"
    Then the write succeeds

  Scenario: Write triggers a pipeline
    Given a GitLab connector with valid token
    When I write GitLab pipeline_run with project "group/project" and ref "main"
    Then the write succeeds

  Scenario: Health check with invalid token returns error
    Given a GitLab connector with invalid token
    When I check the connector health
    Then the health result ok is false
    And the health result detail describes the error

  Scenario: Health check with network error returns error
    Given a GitLab connector with valid token
    When the GitLab API is unreachable
    Then the health result ok is false
    And the health result detail describes the error

  Scenario: Unsupported resource raises error
    Given a GitLab connector with valid token
    When I query resource "unsupported_resource"
    Then the result is an error
