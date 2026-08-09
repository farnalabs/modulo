Feature: GitHub Connector
  As a pipeline author
  I want to interact with GitHub via the connector
  So that I can read repos, files, PRs and write files

  Scenario: Query repositories returns results
    Given a GitHub connector with valid token
    When I query resource "repos" with limit 5
    Then the result has records
    And the records contain repository metadata

  Scenario: Query results expose the rate-limit budget
    Given a GitHub connector with valid token
    When I query resource "repos" with limit 5
    Then the result has records
    And the result exposes the rate-limit budget

  Scenario: Query a file by repo and path
    Given a GitHub connector with valid token
    When I query resource "file" with filters repo "owner/repo" and path "README.md"
    Then the result has records
    And the record contains file content

  Scenario: Query pull requests by repo and state
    Given a GitHub connector with valid token
    When I query resource "pulls" with filters repo "owner/repo" and state "open"
    Then the result has records

  Scenario: Write to a file creates content
    Given a GitHub connector with valid token
    When I write resource "file" with content "base64content" and path "docs/new.md"
    Then the write succeeds

  Scenario: Unsupported resource raises an error
    Given a GitHub connector with valid token
    When I query resource "invalid"
    Then the result is an error

  Scenario: Query returns error with 429 rate limit
    Given a GitHub connector with valid token
    When the API returns HTTP 429 "Rate limit exceeded"
    Then the connector raises a ValueError with "429"

  Scenario: Exhausted 429 surfaces the rate-limit quota headers
    Given a GitHub connector with valid token
    When the GitHub API returns exhausted 429 with quota "X-RateLimit-Reset=1754000000"
    Then the connector raises a ValueError with "429" and the quota header "X-RateLimit-Reset=1754000000"

  Scenario: Query returns error with 500 server error
    Given a GitHub connector with valid token
    When the API returns HTTP 500 "Server Error"
    Then the connector raises a ValueError with "500"

  Scenario: Write returns error with 422 unprocessable
    Given a GitHub connector with valid token
    When writing a file to GitHub returns HTTP 422 "Unprocessable"
    Then the connector raises a ValueError with "422"

  Scenario: Create a pull request
    Given a GitHub connector with valid token
    When I write resource "pr" with head "feature" and base "main"
    Then the write succeeds

  Scenario: Comment on a pull request
    Given a GitHub connector with valid token
    When I write resource "pr_comment" on pull number 1
    Then the write succeeds

  Scenario: Merge a pull request
    Given a GitHub connector with valid token
    When I write resource "pr_merge" on pull number 1
    Then the write succeeds

  Scenario: Request reviewers on a pull request
    Given a GitHub connector with valid token
    When I write resource "pr_review_request" on pull number 1
    Then the write succeeds

  Scenario: Add labels to a pull request
    Given a GitHub connector with valid token
    When I write resource "pr_label" on pull number 1
    Then the write succeeds

  Scenario: Assign an issue
    Given a GitHub connector with valid token
    When I write resource "issue_assign" on issue number 42
    Then the write succeeds

  Scenario: Get a pull request diff
    Given a GitHub connector with valid token
    When I query resource "pr_diff" with filters repo "owner/repo" and pull number 1
    Then the result has records

  Scenario: Search issues
    Given a GitHub connector with valid token
    When I query resource "search_issues" with search query "repo:owner/repo is:open"
    Then the result has records

  Scenario: Query recursive tree listing
    Given a GitHub connector with valid token
    When I query GitHub tree for repo "owner/repo" with path "src" and recursive
    Then the result has records
    And the tree result contains nested entries

  Scenario: Path traversal on file query is blocked
    Given a GitHub connector with valid token
    When I query resource "file" with filters repo "owner/repo" and path "../../etc/passwd"
    Then the result is an error containing "path traversal"

  Scenario: Path traversal on file write is blocked
    Given a GitHub connector with valid token
    When I write resource "file" with content "base64content" and path "../escape.md"
    Then the write is an error containing "path traversal"

  Scenario: Write a batch commit applies file actions
    Given a GitHub connector with valid token
    When I write GitHub files batch for repo "owner/repo"
    Then the write succeeds
    And the batch write reports a commit sha

  Scenario: Batch commit with empty actions is an error
    Given a GitHub connector with valid token
    When I write GitHub files batch for repo "owner/repo" with no actions
    Then the write is an error containing "non-empty 'actions' list"

  Scenario: Batch commit path traversal is blocked
    Given a GitHub connector with valid token
    When I write GitHub files batch for repo "owner/repo" with traversal path "../evil.txt"
    Then the write is an error containing "path traversal"

  Scenario: Query results expose rate-limit budget metadata
    Given a GitHub connector with valid token
    When I query resource "repos" with limit 5
    Then the query result exposes rate-limit metadata

  Scenario: Query the rate-limit budget directly
    Given a GitHub connector with valid token
    When I query resource "rate_limit"
    Then the result has records
    And the query result exposes rate-limit metadata

  Scenario: Rate-limited response reports quota detail
    Given a GitHub connector with valid token
    When the GitHub API is rate limited with zero remaining quota
    Then the connector raises a ValueError with "quota"
    And the connector raises a ValueError with "X-RateLimit-Remaining"

  Scenario: Health check detects an expired token
    Given a GitHub connector with valid token
    And a GitHub connector whose health check reports an expired token
    When I perform a health check
    Then the health result indicates failure
    And the health result detail describes an expired token

  Scenario: Health check reports missing scopes with machine-readable codes
    Given a GitHub connector with valid token
    And a GitHub connector whose health check reports missing scope "repo"
    When I perform a health check
    Then the health result indicates failure
    And the health result detail contains "missing_scope:repo"

  Scenario: Health check passes for a fine-grained PAT
    Given a GitHub connector with valid token
    And a GitHub connector whose health check detects a fine-grained PAT
    When I perform a health check
    Then the health result is ok
    And the health result detail contains "fine-grained"

  Scenario: Query surfaces a typed auth error with a machine-readable code
    Given a GitHub connector with valid token
    When the GitHub API returns HTTP 401 with an expired token
    Then the connector raises a GitHub error with code "token_expired"

  Scenario: Query surfaces a rate-limit error with a machine-readable code
    Given a GitHub connector with valid token
    When the GitHub API returns HTTP 429 with exhausted quota
    Then the connector raises a GitHub error with code "rate_limited"
