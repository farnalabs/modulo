Feature: System Admin — User Management Across Orgs
  As a system admin
  I want to create users in any organisation
  So that I can provision tenant accounts

  Scenario: System admin creates user in specific org
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I create a user with email "user@acme.com" in org "acme-corp"
    Then the user is created successfully
    And the user belongs to org "acme-corp"

  Scenario: Regular admin cannot create user in another org
    Given I am authenticated as an org admin in org "my-org"
    When I attempt to create a user in org "other-org"
    Then I receive a 403 Forbidden error

  Scenario: System admin cannot create a user already a member of the org
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    And a user with email "taken@acme.com" is already a member of org "acme-corp"
    When I create a user with email "taken@acme.com" in org "acme-corp"
    Then I receive a 409 Conflict error

  Scenario: System admin cannot create a user owned by a local account in another org
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    And a local account with email "roaming@acme.com" holds a password in another org
    When I create a user with email "roaming@acme.com" in org "acme-corp"
    Then I receive a 409 Conflict error

  Scenario: System admin cannot create a user with an invalid role
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I create a user with email "badrole@acme.com" role "superadmin" in org "acme-corp"
    Then I receive a 422 Unprocessable Entity error

  Scenario: System admin cannot create a user with a weak password
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I create a user with email "weak@acme.com" password "12345678" in org "acme-corp"
    Then I receive a 422 Unprocessable Entity error

  Scenario: Creating a user in a missing org returns not found
    Given I am authenticated as a system admin
    And an organisation "ghost-org" exists
    When I create a user with email "ghost@acme.com" in missing org "ghost-org"
    Then I receive a 404 Not Found error
