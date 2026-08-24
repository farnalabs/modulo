Feature: Remy Chat Sessions
  As a user of the Remy AI assistant
  I want to manage my chat sessions via the REST API
  So that I can organise my conversations

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a new chat session with provider and model
    When I create a remy session with provider "anthropic" and model "claude-sonnet-4-20250514"
    Then the response status is 201
    And the response contains a session with provider "anthropic"
    And the session has a user_id
    And the session has a context_window_tokens of 200000

  Scenario: List user's sessions shows most recent first
    Given I have 2 remy sessions
    When I list remy sessions
    Then the response status is 200
    And the response contains a paginated list of sessions
    And the sessions are ordered by updated_at descending

  Scenario: Get session by ID returns the session with message count
    Given I have a remy session
    When I get the remy session by id
    Then the response status is 200
    And the response contains the session
    And the response includes the message_count

  Scenario: Get non-existent session returns 404
    When I get a remy session by id "00000000-0000-0000-0000-000000099999"
    Then the response status is 404

  Scenario: Rename an existing session
    Given I have a remy session
    When I rename the remy session to "My renamed chat"
    Then the response status is 200
    And the response contains a session with name "My renamed chat"

  Scenario: Delete a session cascades to messages
    Given I have a remy session with 3 messages
    When I delete the remy session
    Then the response status is 200
    And the session is marked as deleted
    And the session's messages are deleted

  Scenario: Session belongs to other user returns 404
    Given I am authenticated as a viewer in org "acme"
    When I get a remy session that belongs to another user
    Then the response status is 404

  Scenario: Empty session list returns empty items
    When I list remy sessions
    Then the response status is 200
    And the response contains a paginated list of sessions
    And the items list is empty
