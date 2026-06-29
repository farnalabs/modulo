Feature: Enterprise Gate Enforcement
  As an admin
  I want enterprise features to be gated behind a valid license key
  So that only licensed organisations can access SSO, RBAC, audit, and spend limits

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: SSO blocked without enterprise license
    Given I do not have an enterprise license
    When I GET /api/v1/admin/sso/providers
    Then the response status is 402
    And the error detail mentions "sso"

  Scenario: Team RBAC blocked without enterprise license
    Given I do not have an enterprise license
    When I GET /api/v1/teams
    Then the response status is 402
    And the error detail mentions "team_rbac"

  Scenario: Audit viewer blocked without enterprise license
    Given I do not have an enterprise license
    When I GET /api/v1/admin/audit
    Then the response status is 402
    And the error detail mentions "audit_viewer"

  Scenario: Spend limits blocked without enterprise license
    Given I do not have an enterprise license
    When I GET /api/v1/admin/costs/limits
    Then the response status is 402
    And the error detail mentions "admin_spend_limits"

  Scenario: All gates pass with valid enterprise license
    Given I have a valid enterprise license
    When I GET /api/v1/admin/sso/providers
    Then the response status is 200
    When I GET /api/v1/teams
    Then the response status is 200
    When I GET /api/v1/admin/audit
    Then the response status is 200
    When I GET /api/v1/admin/costs/limits
    Then the response status is 200

  Scenario: Free tier features are accessible without a license
    Given I do not have an enterprise license
    When I GET /api/v1/pipelines
    Then the response status is 200
    When I GET /api/v1/changelog
    Then the response status is 200

  Scenario: License expiry degrades enterprise features to free tier
    Given I have an expired enterprise license
    When I GET /api/v1/admin/sso/providers
    Then the response status is 402
    And the error detail mentions "sso"
    When I GET /api/v1/pipelines
    Then the response status is 200

  Scenario: Mixed gating within admin cost endpoints
    Given I do not have an enterprise license
    When I GET /api/v1/admin/costs/limits
    Then the response status is 402
    And the error detail mentions "admin_spend_limits"
    When I GET /api/v1/admin/costs
    Then the response status is 200
    And the response does not contain 402 error
