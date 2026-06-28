Feature: Rate Limiting
  As a platform operator
  I want to limit the rate of model backend requests
  So that costs are controlled and backend capacity is shared fairly

  Scenario: Request within limit is allowed
    Given an org with a per-minute token budget of 100
    When a model backend request is made
    Then the request is allowed

  Scenario: Request over limit is denied
    Given an org with a per-minute token budget of 100
    And the budget is exhausted
    When a model backend request is made
    Then the request is denied with a rate-limit error

  Scenario: Rate limit resets after window
    Given an org with a per-minute token budget of 100
    And the budget is exhausted
    When the rate limit window resets
    And a model backend request is made
    Then the request is allowed again

  Scenario: Rate limit bypass token works
    Given a valid rate limit bypass token
    And the budget is exhausted
    When a request is made with the bypass token
    Then the request is allowed
