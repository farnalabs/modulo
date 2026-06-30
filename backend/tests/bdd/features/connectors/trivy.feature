Feature: Trivy Connector
  As a pipeline author
  I want to interact with Trivy via the connector
  So that I can scan artifacts, query reports, and check server health

  Scenario: Health check returns ok
    Given a Trivy connector
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with connection error
    Given a Trivy connector
    And the Trivy server is unreachable
    When I perform a health check
    Then the health result is not ok

  Scenario: Query artifact with image returns results
    Given a Trivy connector
    When I query Trivy resource "artifact" with image "alpine:3.18"
    Then the result has records

  Scenario: Query artifact with filesystem returns results
    Given a Trivy connector
    When I query Trivy resource "artifact" with filesystem "/"
    Then the result has records

  Scenario: Query artifact with repository returns results
    Given a Trivy connector
    When I query Trivy resource "artifact" with repository "https://github.com/aquasecurity/trivy"
    Then the result has records

  Scenario: Query artifact missing target raises error
    Given a Trivy connector
    When I query Trivy resource "artifact" without target
    Then the result is an error

  Scenario: Query reports list returns results
    Given a Trivy connector
    When I query Trivy resource "reports" with limit 5
    Then the result has records

  Scenario: Query a single report by digest
    Given a Trivy connector
    When I query Trivy resource "report" with digest "sha256:abc123"
    Then the result has records

  Scenario: Query report missing digest raises error
    Given a Trivy connector
    When I query Trivy resource "report" without digest
    Then the result is an error

  Scenario: Query server status
    Given a Trivy connector
    When I query Trivy resource "status"
    Then the result has records

  Scenario: Query plugins list
    Given a Trivy connector
    When I query Trivy resource "plugins"
    Then the result has records

  Scenario: Query unsupported resource raises error
    Given a Trivy connector
    When I query Trivy resource "unknown"
    Then the result is an error

  Scenario: Write scan with image succeeds
    Given a Trivy connector
    When I write Trivy resource "scan" with image "alpine:3.18"
    Then the write succeeds

  Scenario: Write scan missing target raises error
    Given a Trivy connector
    When I write Trivy resource "scan" without target
    Then the result is an error

  Scenario: Write unsupported resource raises error
    Given a Trivy connector
    When I write Trivy resource "invalid"
    Then the result is an error
