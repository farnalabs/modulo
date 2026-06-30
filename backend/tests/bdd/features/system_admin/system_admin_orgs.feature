Feature: System Admin — Organisation Management
  As a system admin
  I want to create and manage organisations
  So that I can provision isolated tenants

  Scenario: System admin creates a new org
    Given I am authenticated as a system admin
    When I create an organisation with name "Acme Corp" and slug "acme-corp"
    Then the organisation is created successfully
    And it has status "active"

  Scenario: Regular admin cannot create an org
    Given I am authenticated as an org admin
    When I attempt to create an organisation with name "Acme Corp" and slug "acme-corp"
    Then I receive a 403 Forbidden error

  Scenario: Duplicate org slug is rejected
    Given I am authenticated as a system admin
    And an organisation with slug "acme-corp" already exists
    When I attempt to create an organisation with slug "acme-corp"
    Then I receive a 409 Conflict error
