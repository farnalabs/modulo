Feature: Tier Catalog
  As a tenant user
  I want to list the plan tiers available to my organisation
  So that I can see which tiers and features are available

  Background:
    Given the tier catalog contains the standard Community and Team tiers

  Scenario: Admin lists all plan tiers ordered by rank
    Given I am authenticated as an admin in org "acme"
    When I request GET /api/v1/admin/tiers
    Then the response status is 200
    And the tiers are ordered by rank
    And each tier has tier_id, label, rank, requires_license, and description fields

  Scenario: Authenticated non-admin can list tiers
    Given I am authenticated as a viewer in org "acme"
    When I request GET /api/v1/admin/tiers
    Then the response status is 200

  Scenario: Unauthenticated request to list tiers is rejected
    When I request GET /api/v1/admin/tiers without authentication
    Then the response status is 401

  Scenario: Empty tier catalog returns an empty tier list
    Given the tier catalog is empty
    And I am authenticated as an admin in org "acme"
    When I request GET /api/v1/admin/tiers
    Then the response status is 200
    And the tiers array is empty

  Scenario: Tier listing survives a programming error as 501
    Given the tier query raises a programming error
    And I am authenticated as an admin in org "acme"
    When I request GET /api/v1/admin/tiers
    Then the response status is 501

  Scenario: Tier listing survives a database error as 503
    Given the tier query raises a database error
    And I am authenticated as an admin in org "acme"
    When I request GET /api/v1/admin/tiers
    Then the response status is 503
