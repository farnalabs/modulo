Feature: Organisation Settings
  As an org admin
  I want to manage organisation settings, members, and API keys
  So that I can control access and configuration for my team

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: View organisation settings page
    Given I am on the organisation settings page
    Then I see the organisation name and member list

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Update organisation name
    Given I am on the organisation settings page
    When I change the organisation name to "Acme Corp"
    Then the organisation name is updated

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Invite a new member
    Given I am on the organisation settings page
    When I click "Add Member"
    Then I see a member invitation form

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Revoke an API key
    Given the organisation has 2 API keys
    When I revoke an API key
    Then the API key status changes to revoked

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Non-admin cannot access settings
    Given I am on the organisation settings page as a viewer
    Then I see a permission denied message
