Feature: SCIM 2.0 Provisioning
  As an enterprise IdP administrator
  I want to provision users and groups via SCIM 2.0
  So that team membership is synced automatically from the identity provider

  Background:
    Given I am authenticated as a SCIM client for org "acme"
    And the enterprise license is valid

  Scenario: Create a SCIM user provisions a new Modulo user
    When I POST /scim/v2/Users with SCIM user "jane@example.com"
    Then the response status is 201
    And the response contains a SCIM User resource
    And the SCIM user has userName "jane@example.com"
    And the SCIM user has a permanent id

  Scenario: Get a SCIM user by id returns the full resource
    Given a SCIM user exists with id "user-001"
    When I GET /scim/v2/Users/user-001
    Then the response status is 200
    And the response contains a SCIM User resource
    And the SCIM user has userName "jane@example.com"

  Scenario: Replace a SCIM user updates all attributes
    Given a SCIM user exists with id "user-001"
    When I PUT /scim/v2/Users/user-001 with SCIM user "jane.updated@example.com"
    Then the response status is 200
    And the SCIM user has userName "jane.updated@example.com"

  Scenario: Delete a SCIM user deactivates the Modulo user
    Given a SCIM user exists with id "user-001"
    When I DELETE /scim/v2/Users/user-001
    Then the response status is 204
    And the Modulo user is deactivated

  Scenario: JIT provisioning links an unknown SCIM user to a new Modulo user
    When I POST /scim/v2/Users with SCIM user "newcomer@example.com"
    Then the response status is 201
    And a Modulo user is created with auth_provider "scim"
    And the Modulo user has no password set

  Scenario: Deprovisioning a SCIM user deactivates but preserves the record
    Given a SCIM user exists with id "user-001"
    When I PATCH /scim/v2/Users/user-001 with active=false
    Then the response status is 200
    And the Modulo user active flag is false
    And the Modulo user record still exists

  Scenario: Create a SCIM group with members
    Given a SCIM user exists with id "user-001"
    When I POST /scim/v2/Groups with SCIM group "Engineering" containing user "user-001"
    Then the response status is 201
    And the response contains a SCIM Group resource
    And the SCIM group has displayName "Engineering"
    And the SCIM group has 1 member

  Scenario: Team sync maps IdP group membership to Modulo teams
    Given a SCIM user exists with id "user-001"
    And a SCIM group exists with id "group-001" and displayName "Engineering"
    When I PATCH /scim/v2/Groups/group-001 add member "user-001"
    Then the response status is 200
    And the Modulo user "user-001" is a member of team "Engineering"
    When I PATCH /scim/v2/Groups/group-001 remove member "user-001"
    Then the response status is 200
    And the Modulo user "user-001" is not a member of team "Engineering"

  Scenario: SCIM bearer token auth rejects invalid credentials
    When I POST /scim/v2/Users with SCIM user "attacker@example.com" and no auth token
    Then the response status is 401

  Scenario: Enterprise license gate blocks SCIM without valid license
    Given I do not have an enterprise license
    When I GET /scim/v2/Users
    Then the response status is 402
