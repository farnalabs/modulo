Feature: Deployment Metadata
  As an operator
  I want to fetch deployment metadata
  So that I can verify build and runtime information about the running instance

  Scenario: GET deployment metadata returns 200
    When I GET /api/v1/deployment
    Then the response status is 200

  Scenario: Response contains all required fields
    When I GET /api/v1/deployment
    Then the response contains deployment metadata fields

  Scenario: Version is a non-empty string
    When I GET /api/v1/deployment
    Then the "version" field is a non-empty string

  Scenario: Uptime is a non-negative integer
    When I GET /api/v1/deployment
    Then the "uptime_seconds" field is a non-negative integer

  Scenario: Environment defaults to development
    When I GET /api/v1/deployment
    Then the "environment" field is "development"

  Scenario: Build metadata fields are strings
    When I GET /api/v1/deployment
    Then build metadata fields are strings

  Scenario: Build metadata falls back to empty when env vars not set
    When I GET /api/v1/deployment
    Then git_sha is empty and ci_job_url is empty
