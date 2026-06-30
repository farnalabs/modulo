Feature: Team Gate Enforcement
  As an admin
  I want Team features to be gated behind a valid license key
  So that only licensed organisations can access SSO, RBAC, audit, and spend limits

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: SSO blocked without Team license
    Given I do not have a Team license
    When I GET /api/v1/admin/sso/providers
    Then the response status is 402
    And the error detail mentions "sso"

  Scenario: Team RBAC blocked without Team license
    Given I do not have a Team license
    When I GET /api/v1/teams
    Then the response status is 402
    And the error detail mentions "team_rbac"

  Scenario: Audit viewer blocked without Team license
    Given I do not have a Team license
    When I GET /api/v1/admin/audit
    Then the response status is 402
    And the error detail mentions "audit_viewer"

  Scenario: Spend limits blocked without Team license
    Given I do not have a Team license
    When I GET /api/v1/admin/costs/limits
    Then the response status is 402
    And the error detail mentions "admin_spend_limits"

  Scenario: All gates pass with valid Team license
    Given I have a valid Team license
    When I GET /api/v1/admin/sso/providers
    Then the response status is 200
    When I GET /api/v1/teams
    Then the response status is 200
    When I GET /api/v1/admin/audit
    Then the response status is 200
    When I GET /api/v1/admin/costs/limits
    Then the response status is 200

  Scenario: Community tier features are accessible without a license
    Given I do not have a Team license
    When I GET /api/v1/pipelines
    Then the response status is 200
    When I GET /api/v1/changelog
    Then the response status is 200

  Scenario: License expiry degrades Team features to Community tier
    Given I have an expired Team license
    When I GET /api/v1/admin/sso/providers
    Then the response status is 402
    And the error detail mentions "sso"
    When I GET /api/v1/pipelines
    Then the response status is 200

  Scenario: Mixed gating within admin cost endpoints
    Given I do not have a Team license
    When I GET /api/v1/admin/costs/limits
    Then the response status is 402
    And the error detail mentions "admin_spend_limits"
    When I GET /api/v1/admin/costs
    Then the response status is 200
    And the response does not contain 402 error
