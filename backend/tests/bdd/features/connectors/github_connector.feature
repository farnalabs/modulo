Feature: GitHub Connector
  As a pipeline author
  I want to interact with GitHub via the connector
  So that I can read repos, files, PRs and write files

  Scenario: Query repositories returns results
    Given a GitHub connector with valid token
    When I query resource "repos" with limit 5
    Then the result has records
    And the records contain repository metadata

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

  Scenario: Query the repository tree recursively
    Given a GitHub connector with valid token
    When I query resource "tree" with filters repo "owner/repo" and branch "main"
    Then the result has records
    And the records contain tree entries

  Scenario: Query the repository tree with a path filter
    Given a GitHub connector with valid token
    When I query resource "tree" with filters repo "owner/repo", branch "main" and path "src"
    Then the result has records
    And every tree record path is under "src"

  Scenario: Path traversal on file read is blocked
    Given a GitHub connector with valid token
    When I query resource "file" with filters repo "owner/repo" and path "../README.md"
    Then the result is an error with "path traversal blocked"

  Scenario: Path traversal on file write is blocked
    Given a GitHub connector with valid token
    When I write resource "file" with content "SGVsbG8=" and path "../../etc/passwd"
    Then the result is an error with "path traversal blocked"

  Scenario: Writing plain-text file content base64-encodes it
    Given a GitHub connector with valid token
    When I write resource "file" with text content "Hello, world!" and path "docs/hello.txt"
    Then the file write payload is base64 of "Hello, world!"

  Scenario: Reading a file exposes decoded content
    Given a GitHub connector with valid token
    When I query resource "file" with filters repo "owner/repo" and path "README.md"
    Then the record contains file content
    And the record decoded content is "my readme"
