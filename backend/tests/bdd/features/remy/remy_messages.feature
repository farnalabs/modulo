Feature: Remy Chat Messages
  As a user of the Remy AI assistant
  I want to append and list messages in my chat sessions
  So that I can carry on a conversation

  Scenario: Append a user message to an existing session
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    When I append a "user" message with content "Hello, Remy!"
    Then the response status is 201
    And the response contains a message with role "user"
    And the message has the session_id set

  Scenario: Append an assistant message
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    When I append a "assistant" message with content "Hi there!"
    Then the response status is 201
    And the response contains a message with role "assistant"

  Scenario: Append a tool_result message
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    When I append a "tool_result" message with content '{"status": "ok"}'
    Then the response status is 201
    And the response contains a message with role "tool_result"

  Scenario: Append a summary message
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    When I append a "summary" message with content "Conversation summary here"
    Then the response status is 201
    And the response contains a message with role "summary"

  Scenario: List messages for a session returns ascending order
    Given I am authenticated as an admin in org "acme"
    And I have a remy session with messages in order
    When I list messages for the remy session
    Then the response status is 200
    And the response contains a paginated list of messages
    And the messages are ordered by created_at ascending

  Scenario: Append message with parent_id for branching
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    And I have a parent message in the session
    When I append a "user" message with content "follow-up" and parent_id set
    Then the response status is 201
    And the response contains a message with parent_id matching the parent

  Scenario: Invalid role is rejected with 422
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    When I append a message with role "invalid_role"
    Then the response status is 422

  Scenario: Append to non-existent session returns 404
    Given I am authenticated as an admin in org "acme"
    When I append a "user" message to session "00000000-0000-0000-0000-000000099999"
    Then the response status is 404

  Scenario: List messages for non-existent session returns 404
    Given I am authenticated as an admin in org "acme"
    When I list messages for session "00000000-0000-0000-0000-000000099999"
    Then the response status is 404

  Scenario: Append message with tool_calls_json
    Given I am authenticated as an admin in org "acme"
    And I have a remy session
    When I append a "assistant" message with tool_calls containing a code_interpreter call
    Then the response status is 201
    And the response contains tool_calls_json with a tool_call entry
