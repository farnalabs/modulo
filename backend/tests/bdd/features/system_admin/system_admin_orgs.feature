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

  Scenario: System admin deletes an org
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I delete the organisation
    Then the organisation is deleted

  Scenario: Deleting a missing org returns not found
    Given I am authenticated as a system admin
    And an organisation "missing" exists
    When I delete a missing organisation
    Then I receive a 404 Not Found error

  Scenario: Regular admin cannot delete an org
    Given I am authenticated as an org admin
    And an organisation "acme-corp" exists
    When I attempt to delete the organisation
    Then I receive a 403 Forbidden error

  Scenario: System admin lists all organisations
    Given I am authenticated as a system admin
    And the system has several organisations
    When I list all organisations
    Then I see all organisations

  Scenario: Reserved infrastructure orgs are hidden from the list
    Given I am authenticated as a system admin
    And the system holds reserved infrastructure orgs
    When I list all organisations
    Then reserved infrastructure orgs are hidden

  Scenario: Regular admin cannot list organisations
    Given I am authenticated as an org admin
    When I attempt to list all organisations
    Then I receive a 403 Forbidden error

  Scenario: System admin views an org license falling back to the system license
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I view the organisation's license
    Then the organisation license falls back to the system license

  Scenario: System admin views an org license resolved from the org's own key
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    And the organisation holds its own license key
    When I view the organisation's license
    Then the organisation license is resolved from its own key

  Scenario: An org with an invalid stored license key falls back to the system license
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    And the organisation holds an invalid license key
    When I view the organisation's license
    Then the organisation license falls back to the system license

  Scenario: Viewing the license of a missing org returns not found
    Given I am authenticated as a system admin
    And an organisation "missing" exists
    When I view a missing organisation's license
    Then I receive a 404 Not Found error

  Scenario: System admin sets an org license key
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I set a valid license key "abc.123" on the organisation
    Then the organisation license is set to "abc.123"

  Scenario: Setting an invalid org license key is rejected
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    When I set an invalid license key "bad-key" on the organisation
    Then I receive a 422 Unprocessable Entity error

  Scenario: Setting a license on a missing org returns not found
    Given I am authenticated as a system admin
    And an organisation "missing" exists
    When I set a valid license key "abc.123" on a missing organisation
    Then I receive a 404 Not Found error

  Scenario: System admin removes an org license key
    Given I am authenticated as a system admin
    And an organisation "acme-corp" exists
    And the organisation holds its own license key
    When I remove the organisation's license
    Then the organisation license is removed

  Scenario: Removing a license from a missing org returns not found
    Given I am authenticated as a system admin
    And an organisation "missing" exists
    When I remove a missing organisation's license
    Then I receive a 404 Not Found error

  Scenario: Regular org admin cannot view or manage org licenses
    Given I am authenticated as an org admin
    And an organisation "acme-corp" exists
    When I attempt to view the organisation's license
    Then I receive a 403 Forbidden error
    When I attempt to set a license "abc.123" on the organisation
    Then I receive a 403 Forbidden error
    When I attempt to remove the organisation's license
    Then I receive a 403 Forbidden error
