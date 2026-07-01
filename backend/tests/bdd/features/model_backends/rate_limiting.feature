Feature: API Rate Limiting
  As a platform operator
  I want to rate-limit high-value endpoints
  So that the system is protected from abuse and resource exhaustion

  Background:
    Given rate limiting is enabled
    And the system has Redis available for distributed rate limiting

  Scenario: POST /api/v1/runs is rate limited at 60 requests per minute per API key
    When I send 60 POST requests to /api/v1/runs within 60 seconds
    Then the response status is 200
    When I send 1 more POST request to /api/v1/runs within the same window
    Then the response status is 429
    And the response has a "Retry-After" header
    And the response body contains "rate_limit_exceeded"

  Scenario: GET requests to /api/v1/runs are not rate limited
    When I send 100 GET requests to /api/v1/runs
    Then all responses have status 200

  Scenario: Webhook trigger is rate limited at 100 requests per minute per trigger
    When I send 100 POST requests to /api/v1/webhooks/dummy-trigger within 60 seconds
    Then the response status is 200
    When I send 1 more POST request within the same window
    Then the response status is 429

  Scenario: MCP tool calls are rate limited at 200 requests per minute per client
    When I send 200 POST requests to /mcp/any-tool within 60 seconds
    Then the response status is 200
    When I send 1 more POST request within the same window
    Then the response status is 429

  Scenario: Rate limit resets after the window expires
    Given I have exceeded the rate limit on /api/v1/runs
    When 60 seconds have passed
    Then a new POST request to /api/v1/runs succeeds

  Scenario: Rate limit bypass token skips rate limiting
    Given a valid MODULO_RATELIMIT_BYPASS_TOKEN is configured
    When I send a POST request to /api/v1/runs with the bypass token
    Then the request is allowed even if the rate limit would be exceeded

  Scenario: Rate limits are configurable at runtime
    When I PUT /api/v1/admin/rate-limits with new rules
    Then the rate limit rules are updated
    And subsequent requests use the new limits

  Scenario: Non-admin cannot update rate limit rules
    Given I am authenticated as a viewer
    When I PUT /api/v1/admin/rate-limits with new rules
    Then the response status is 403

  Scenario: Empty rules are rejected
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/rate-limits with empty rules
    Then the response status is 400

  Scenario: In-memory fallback works when Redis is unavailable
    Given Redis is not available
    When I send requests to /api/v1/runs
    Then rate limiting still works with in-memory token bucket
    And a startup warning is logged

  Scenario: Rate limiting is disabled in SQLite mode
    Given the database is SQLite
    When I send POST requests to /api/v1/runs
    Then no rate limiting is applied
