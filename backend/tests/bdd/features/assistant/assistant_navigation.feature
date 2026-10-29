Feature: Assistant navigation commands
  The Assistant resolves navigation tool calls emitted by the LLM against the
  app manifest. Navigation is read-only: there is no browser automation,
  permission prompt, or command round-trip to the frontend.

  Scenario: Assistant resolves a navigate tool call
    When the LLM emits a "navigate" tool call with path "/admin/pipelines"
    Then the backend yields a tool_call event for the navigation tool
    And the navigation result carries the route details

  Scenario: Assistant resolves a get_manifest tool call for one route
    When the LLM emits a "get_manifest" tool call with path "/admin/pipelines"
    Then the backend yields a tool_call event for the navigation tool
    And the navigation result carries the route details
