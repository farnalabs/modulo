Feature: License Management
  As an admin
  I want to upload and inspect Ed25519-signed license keys
  So that I can manage tier gating and view license status

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Upload valid enterprise license
    Given I have a signed enterprise license key
    When I POST the license key to /api/v1/admin/license
    Then the response status is 200
    And the response contains tier "enterprise"
    And the response contains features "sso, team_rbac, audit_viewer, admin_spend_limits"

  Scenario: Invalid signature rejected
    Given I have a tampered license key
    When I POST the license key to /api/v1/admin/license
    Then the response status is 422
    And the error detail mentions "Signature"

  Scenario: Expired license rejected
    Given I have an expired license key
    When I POST the license key to /api/v1/admin/license
    Then the response status is 422
    And the error detail mentions "expired"

  Scenario: Free tier when no license uploaded
    Given I do not have a license
    When I GET /api/v1/admin/license
    Then the response status is 200
    And the response shows free tier
    And the response has_license is false

  Scenario: License status displayed after upload
    Given I have stored a valid enterprise license
    When I GET /api/v1/admin/license
    Then the response status is 200
    And the response has_license is true
    And the response contains tier "enterprise"
    And the response contains org_id "acme-org"

  Scenario: Enterprise features unlocked after license upload
    Given I have stored a valid enterprise license
    When I GET /api/v1/admin/license
    Then the response features include "sso"
    And the response features include "team_rbac"

  Scenario: License badge data returned by API
    Given I have stored a valid enterprise license with a known expiry
    When I GET /api/v1/admin/license
    Then the response status is 200
    And the response contains tier "enterprise"
    And the response contains an expires_at date
    And the response has_license is true

  Scenario: Non-admin cannot manage license
    Given I am authenticated as a non-admin user
    When I GET /api/v1/admin/license
    Then the response status is 403
    When I POST a license key to /api/v1/admin/license
    Then the response status is 403
