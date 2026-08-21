Feature: Feature Flag Inspection
  As an organisation admin
  I want to inspect feature flags and their current status
  So that I can understand which features are available on my plan

  Background:
    Given I am authenticated as an admin in org "acme"
    And the database has feature flags configured

  Scenario: List all feature flags
    When I GET "/api/v1/admin/feature-flags"
    Then the response status is 200
    And the response contains a "license" object with "tier", "has_license_key", and "is_valid" fields
    And the response contains a "flags" array
    And each flag in the "flags" array has "name", "description", "tier", and "currently_active" fields

  Scenario: View a specific flag
    When I GET "/api/v1/admin/feature-flags/sso"
    Then the response status is 200
    And the response field "name" equals "sso"
    And the response field "tier" equals "team"

  Scenario: Unknown flag returns 404
    When I GET "/api/v1/admin/feature-flags/nonexistent_flag"
    Then the response status is 404

  Scenario: Toggle a flag override
    Given I have a valid session
    When I PUT "/api/v1/admin/feature-flags/sso" with body {"enabled": true}
    Then the response status is 200
    And the response contains an "overridden" field

  Scenario: Public license endpoint
    When I GET "/api/v1/license"
    Then the response status is 200
    And the response contains a "tier" field
    And the response contains a "features" list
