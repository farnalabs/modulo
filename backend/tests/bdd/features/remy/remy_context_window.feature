Feature: Remy Context Window Management
  As the Remy AI assistant
  I want to manage conversation context within the model's token budget
  So that conversations never exceed the context window limit

  Scenario: Conversation fits within budget — no pruning
    Given I have a conversation with 3 messages totalling 500 tokens
    And the context window budget is 200000 tokens
    When I reconstruct the conversation context
    Then all 3 messages are kept
    And no pruning occurs

  Scenario: Conversation exceeds budget — pruning occurs
    Given I have a conversation with 10 messages totalling 50000 tokens
    And the context window budget is 10000 tokens (8000 after safety margin)
    When I reconstruct the conversation context
    Then the oldest messages are pruned
    And the system prompt is always preserved
    And the newest user message is always preserved

  Scenario: Pruning removes >50% of messages — summary generated
    Given I have a conversation with 20 messages totalling 50000 tokens
    And the context window budget is 5000 tokens (4000 after safety margin)
    When I reconstruct the conversation context with an API key
    Then a summary of pruned messages is generated
    And the conversation has has_summary set to true

  Scenario: Safety margin is respected — budget is 80% of context_window
    Given a context_window_tokens of 100000
    When I calculate the available budget
    Then the budget is 80000 tokens

  Scenario: Empty conversation returns no messages
    Given I have a conversation with 0 messages
    When I reconstruct the conversation context
    Then the context has only the system message and user message
