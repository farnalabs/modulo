Feature: Auth Brute Force Rate Limiting (PRD §7.18)
  As a platform operator
  I want to rate-limit failed login attempts per IP
  So that brute force attacks are mitigated and legitimate users are not locked out permanently

  Scenario: Login rate limit exceeded returns 429
    Given I have failed to login 10 times from IP "192.168.1.1" in the last minute
    When I attempt to login from IP "192.168.1.1"
    Then the response status is 429
    And the response has a Retry-After header

  Scenario: Login rate limit resets after window expires
    Given I have failed to login 10 times from IP "192.168.1.1" in the last minute
    When 60 seconds pass
    And I attempt to login from IP "192.168.1.1"
    Then the response status is 200

  Scenario: Different IPs have independent rate limits
    Given I have failed to login 10 times from IP "192.168.1.1" in the last minute
    And I have failed to login 0 times from IP "10.0.0.1" in the last minute
    When I attempt to login from IP "10.0.0.1"
    Then the response status is 200

  Scenario: Successful login resets failure counter
    Given I have failed to login 9 times from IP "192.168.1.1" in the last minute
    When I successfully login from IP "192.168.1.1"
    And I attempt to login from IP "192.168.1.1"
    Then the response status is 200

  Scenario: Exponential backoff increases wait time
    Given I have failed to login 20 times from IP "192.168.1.1" in the last minute
    And the current backoff for IP "192.168.1.1" is at least 120 seconds
    When I attempt to login from IP "192.168.1.1"
    Then the Retry-After value is at least 120
