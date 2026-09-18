Feature: Snyk Connector
  As a pipeline author
  I want to interact with Snyk via the connector
  So that I can query projects, issues, vulnerabilities, and manage findings

  Scenario: Health check returns ok
    Given a Snyk connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with invalid token
    Given a Snyk connector with valid token
    And the Snyk API returns unauthorized
    When I perform a health check
    Then the health result is not ok

  Scenario: Query projects returns results
    Given a Snyk connector with valid token
    When I query Snyk resource "projects" with org "my-org"
    Then the result has records

  Scenario: Query a single project returns detail
    Given a Snyk connector with valid token
    When I query Snyk resource "project" with org "my-org" and project "proj-1"
    Then the result has records

  Scenario: Query issues returns results
    Given a Snyk connector with valid token
    When I query Snyk resource "issues" with org "my-org" and project "proj-1"
    Then the result has records

  Scenario: Query organizations returns results
    Given a Snyk connector with valid token
    When I query Snyk resource "orgs" with limit 10
    Then the result has records

  Scenario: Query tests returns results
    Given a Snyk connector with valid token
    When I query Snyk resource "tests" with org "my-org"
    Then the result has records

  Scenario: Query aggregated issues with packages
    Given a Snyk connector with valid token
    When I query Snyk resource "aggregated_issues" with org "my-org" and packages
    Then the result has records

  Scenario: Trigger a test succeeds
    Given a Snyk connector with valid token
    When I write Snyk resource "test" with org "my-org" and package "requests@4.0.0" ecosystem "pypi"
    Then the write succeeds

  Scenario: Ignore an issue succeeds
    Given a Snyk connector with valid token
    When I write Snyk resource "ignore" with org "my-org" project "proj-1" and issue "SNYK-123"
    Then the write succeeds

  Scenario: Missing org_id for projects raises an error
    Given a Snyk connector with valid token
    When I query Snyk resource "projects" without org filter
    Then the result is an error
