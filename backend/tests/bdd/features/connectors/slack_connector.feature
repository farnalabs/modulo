Feature: Slack Connector
  As a pipeline author
  I want to interact with Slack via the connector
  So that I can list channels, read messages, and post messages

  Scenario: List channels returns results
    Given a Slack connector with valid bot token
    When I query resource "channels" with limit 10
    Then the result has records
    And the records contain channel metadata

  Scenario: Get messages from a channel
    Given a Slack connector with valid bot token
    When I query resource "messages" with channel "C12345"
    Then the result has records

  Scenario: List users returns members
    Given a Slack connector with valid bot token
    When I query resource "users" with limit 10
    Then the result has records

  Scenario: Post a message to a channel
    Given a Slack connector with valid bot token
    When I write resource "message" with channel "C12345" and text "Hello"
    Then the write succeeds

  Scenario: Missing channel on messages query raises an error
    Given a Slack connector with valid bot token
    When I query resource "messages" without channel filter
    Then the result is an error
