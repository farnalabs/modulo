Feature: Product Analytics Transparency
  As a system administrator
  I want to inspect how product analytics data is collected and delivered
  So that I can verify the instance's consent level, enforcement state, and delivery health

  Background:
    Given I am authenticated as a system admin

  Scenario: Returns stored transparency state
    Given the system config row "product_analytics_consent_level" has value "all"
    And the system config row "product_analytics_enabled" has value "true"
    And the system config row "product_analytics_enforcement_enabled" has value "true"
    And the system config row "product_analytics_dump_count" has value "42"
    And the system config row "product_analytics_last_dump_at" was "1" days ago
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 200
    And the transparency consent level is "all"
    And the transparency instance is enabled
    And the transparency enforcement is enabled
    And the transparency dump count is 42
    And the transparency last dump matches the configured value

  Scenario: Returns defaults when no analytics config rows exist
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 200
    And the transparency response returns the default state

  Scenario: Warns when consent is all and the last dump is stale
    Given the system config row "product_analytics_consent_level" has value "all"
    And the system config row "product_analytics_enabled" has value "true"
    And the system config row "product_analytics_last_dump_at" was "4" days ago
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 200
    And the transparency response warns "not_reaching_farnalabs"

  Scenario: Does not warn when the last dump is recent
    Given the system config row "product_analytics_consent_level" has value "all"
    And the system config row "product_analytics_last_dump_at" was "1" days ago
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 200
    And the transparency response has no warning

  Scenario: Does not warn when consent is not all
    Given the system config row "product_analytics_consent_level" has value "off"
    And the system config row "product_analytics_last_dump_at" was "30" days ago
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 200
    And the transparency consent level is "off"
    And the transparency response has no warning

  Scenario: Rejects a non-system-admin user with 403
    Given I am authenticated as an org admin
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 403

  Scenario: Rejects an unauthenticated request with 401
    When I GET /api/v1/product-analytics/transparency without authentication
    Then the response status is 401

  Scenario: Maps a programming error to 501
    Given the transparency config lookup fails with a programming error
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 501

  Scenario: Maps a database failure to 503
    Given the transparency config lookup fails with a database error
    When I GET /api/v1/product-analytics/transparency
    Then the response status is 503