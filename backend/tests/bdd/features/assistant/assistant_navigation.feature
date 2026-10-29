Feature: Assistant manifest lookup
  The Assistant resolves read-only manifest tool calls emitted by the LLM
  against the app manifest. There is no browser automation, permission
  prompt, or command round-trip to the frontend, and no tool claims to move
  the user's browser.

  Scenario: Assistant only advertises the read-only manifest tool
    When the assistant builds its navigation tool definitions
    Then the navigation tool list is exactly "get_manifest"

  Scenario: Assistant resolves a get_manifest tool call for one route
    When the LLM emits a "get_manifest" tool call with path "/admin/pipelines"
    Then the backend yields a tool_call event for the navigation tool
    And the navigation result carries the route details

  Scenario: Assistant resolves a get_manifest tool call for the whole app
    When the LLM emits a "get_manifest" tool call with no path
    Then the backend yields a tool_call event for the navigation tool
    And the navigation result carries the full manifest
