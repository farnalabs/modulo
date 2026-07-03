Feature: Remy UI Commands
  Remy can execute browser automation commands by emitting LLM tool calls
  that get forwarded to the frontend via the SSE stream.

  Background:
    Given the organisation has Remy enabled with "safe" permission mode
    And a user with "admin" org role
    And a chat session exists for the user
    And the user has sent a message in that session

  Scenario: Remy navigates to a page
    When the LLM emits a "navigate" tool call with path "/admin/pipelines"
    Then the backend yields an "ui_command_batch" event with the navigate command
    And the frontend executes the navigate command
    And the URL changes to "/admin/pipelines"

  Scenario: Remy clicks an element with destructive selector requires approval
    Given permission mode is "safe"
    When the LLM emits a "click" tool call with selector "[data-testid=delete-btn]"
    Then the backend yields a "permission_request" event
    And the frontend shows the approval card
    When the user approves the action
    Then the backend yields an "ui_command_batch" event with the click command

  Scenario: Remy fills a form field (auto-allowed in safe mode)
    Given permission mode is "safe"
    When the LLM emits a "fill" tool call with selector "#email" and value "test@example.com"
    Then the backend yields an "ui_command_batch" event with the fill command
    And the frontend fills the input field

  Scenario: Remy extracts page content
    When the LLM emits an "extract" tool call
    Then the backend yields an "ui_command_batch" event with the extract command
    And the frontend returns the element's text content

  Scenario: Multi-step workflow (navigate -> wait -> click -> go_back)
    When the LLM emits a sequence of tool calls
    Then each command is yielded as an "ui_command_batch" event
    And the results are fed back to the LLM for the next turn
