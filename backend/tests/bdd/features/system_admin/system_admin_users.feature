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
