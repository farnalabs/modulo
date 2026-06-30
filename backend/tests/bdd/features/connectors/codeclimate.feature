Feature: Code Climate Connector
  As a pipeline author
  I want to interact with Code Climate via the connector
  So that I can query repos, snapshots, test reports, and push test reports

  Scenario: Health check returns GREEN
    Given a Code Climate connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check returns RED
    Given a Code Climate connector with valid token
    And the Code Climate API returns unhealthy status
    When I perform a health check
    Then the health result is not ok

  Scenario: Query repos returns results
    Given a Code Climate connector with valid token
    When I query resource "repos" with limit 10
    Then the result has records

  Scenario: Query repos filtered by github_slug
    Given a Code Climate connector with valid token
    When I query resource "repos" with github_slug "my-org/my-repo"
    Then the result has records

  Scenario: Query a specific repo
    Given a Code Climate connector with valid token
    When I query resource "repo" with id "repo-123"
    Then the result has records

  Scenario: Query snapshots for a repo
    Given a Code Climate connector with valid token
    When I query resource "snapshots" with repo_id "repo-123"
    Then the result has records

  Scenario: Query a specific snapshot
    Given a Code Climate connector with valid token
    When I query resource "snapshot" with repo_id "repo-123" and snapshot_id "ss-456"
    Then the result has records

  Scenario: Query test reports for a repo
    Given a Code Climate connector with valid token
    When I query resource "test_reports" with repo_id "repo-123"
    Then the result has records

  Scenario: Query a specific test report
    Given a Code Climate connector with valid token
    When I query resource "test_report" with repo_id "repo-123" and report_id "tr-789"
    Then the result has records

  Scenario: Write test report succeeds
    Given a Code Climate connector with valid token
    When I write a test report for repo "repo-123" duration 1200 exit_code 0 branch "main" sha "abc123"
    Then the write succeeds

  Scenario: Missing repo_id for repo query raises error
    Given a Code Climate connector with valid token
    When I query resource "repo" without id filter
    Then the result is an error

  Scenario: Missing repo_id for snapshots query raises error
    Given a Code Climate connector with valid token
    When I query resource "snapshots" without repo_id filter
    Then the result is an error

  Scenario: Missing repo_id for test_reports query raises error
    Given a Code Climate connector with valid token
    When I query resource "test_reports" without repo_id filter
    Then the result is an error
