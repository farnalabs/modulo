Feature: Assistant streaming API key requirement
  A streaming request to an Assistant session only reaches the LLM when the
  organisation has an API key configured. When none resolves, the SSE stream
  yields a descriptive error event so the frontend can prompt the user to
  configure their provider key.

  Background:
    Given the organisation has Assistant enabled with "safe" permission mode
    And a user with "admin" org role
    And a chat session exists for the user

  Scenario: Streaming fails when no API key is configured
    Given no model backends exist for the org
    When I check assistant access
    Then the stream reports no API key configured
