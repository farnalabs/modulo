Feature: Slack Connector
  As a pipeline author
  I want to interact with Slack via the connector
  So that I can list channels, read messages, and post messages

  Scenario: List channels returns results
    Given a Slack connector with valid bot token
    When I query resource "channels" with limit 10
    Then the result has records
    And the records contain channel metadata

  Scenario: List channels with cursor pagination
    Given a Slack connector with valid bot token
    When I query resource "channels" with cursor "dXNlcjpVMDIzNTE2NTI"
    Then the result has records

  Scenario: Get messages from a channel
    Given a Slack connector with valid bot token
    When I query resource "messages" with channel "C12345"
    Then the result has records

  Scenario: Get messages with oldest and latest timestamps
    Given a Slack connector with valid bot token
    When I query resource "messages" with channel "C12345" and oldest "1234567890.000000" and latest "1234567899.000000"
    Then the result has records

  Scenario: List users returns members
    Given a Slack connector with valid bot token
    When I query resource "users" with limit 10
    Then the result has records

  Scenario: List users with cursor pagination
    Given a Slack connector with valid bot token
    When I query resource "users" with cursor "dXNlcjpVMDIzNTE2NTI"
    Then the result has records

  Scenario: Post a message to a channel
    Given a Slack connector with valid bot token
    When I write resource "message" with channel "C12345" and text "Hello"
    Then the write succeeds

  Scenario: Missing channel on messages query raises an error
    Given a Slack connector with valid bot token
    When I query resource "messages" without channel filter
    Then the result is an error

  Scenario: Unsupported resource raises an error
    Given a Slack connector with valid bot token
    When I query resource "unknown"
    Then the result is an error

  Scenario: Unsupported write resource raises an error
    Given a Slack connector with valid bot token
    When I write resource "file" with channel "C12345" and text "test"
    Then the write is an error

  Scenario: Missing channel on write raises an error
    Given a Slack connector with valid bot token
    When I write resource "message" with no channel
    Then the write is an error

  Scenario: Health check with valid token returns ok
    Given a Slack connector with valid bot token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with invalid token returns error
    Given a Slack connector with invalid bot token
    When I perform a health check
    Then the health result indicates failure

  Scenario: Health check with non-JSON response returns a failure
    Given a Slack connector with valid bot token
    When the API returns non-JSON response
    Then the health result indicates failure
