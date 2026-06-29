Feature: API Rate Limiting
  As a platform operator
  I want to rate-limit API endpoints by key and endpoint
  So that fair usage is enforced and the system is protected from abuse

  Scenario: Request within rate limit is allowed
    Given I have made 59 requests to POST /api/v1/runs in the last minute
    When I POST /api/v1/runs
    Then the response status is 200

  Scenario: Rate limit exceeded returns 429
    Given I have exceeded my rate limit for POST /api/v1/runs
    When I POST /api/v1/runs
    Then the response status is 429
    And the response has a Retry-After header
    And the response body indicates rate limit exceeded

  Scenario: Rate limit resets after window expires
    Given I have exceeded my rate limit for POST /api/v1/runs
    When 60 seconds pass
    And I POST /api/v1/runs
    Then the response status is 200

  Scenario: Different endpoints have independent rate limits
    Given I have made 60 requests to POST /api/v1/runs in the last minute
    When I POST /api/v1/triggers
    Then the response status is 200

  Scenario: Per-API-key rate limiting isolates counters
    Given API key "mk_key_one" has made 60 requests to POST /api/v1/runs
    And API key "mk_key_two" has made 0 requests to POST /api/v1/runs
    When I POST /api/v1/runs with API key "mk_key_two"
    Then the response status is 200

  Scenario: Retry-After header present on 429 response
    Given I have exceeded my rate limit for POST /api/v1/runs
    When I POST /api/v1/runs
    Then the response includes a Retry-After header
    And the Retry-After value is at least 1

  Scenario: Admin can update rate limit rules at runtime
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/rate-limits with 10 requests per 30 seconds for /api/v1/runs
    Then the response status is 200
    And the rate limit rules include the new /api/v1/runs limit
