Feature: Assistant access control
  As an org admin
  I want to control who can access the Assistant AI assistant
  So that access is restricted to authorised users

  The request-time allow-list gate is ``AssistantConfigService.check_access``:
  a user is granted access when their user_id, org_role, or team_id appears on
  the access list, and denied otherwise. A streaming request also only reaches
  the LLM when the organisation has an API key configured.

  Background:
    Given the organisation has Assistant enabled with "safe" permission mode
    And a user with "admin" org role
    And a chat session exists for the user

  Scenario: Streaming fails when no API key is configured
    Given no model backends exist for the org
    When I check assistant access
    Then the stream reports no API key configured

  Scenario: Access is granted when the user_id is on the access list
    Given the Assistant access list includes my user_id
    When I evaluate assistant access control
    Then access is granted

  Scenario: Access is granted when the org_role is on the access list
    Given the Assistant access list includes role "admin"
    When I evaluate assistant access control
    Then access is granted

  Scenario: Access is granted when a team_id on the access list matches the user's team
    Given the Assistant access list includes team_id "11111111-1111-1111-1111-111111111111"
    And I belong to team "11111111-1111-1111-1111-111111111111"
    When I evaluate assistant access control
    Then access is granted

  Scenario: Access is denied when the user is on no access list
    Given the Assistant access list does not include my role or user_id
    When I evaluate assistant access control
    Then access is denied
