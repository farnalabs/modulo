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

  Scenario: Get channel info
    Given a Slack connector with valid bot token
    When I query resource "channel_info" with channel "C001"
    Then the result has records

  Scenario: Get channel members
    Given a Slack connector with valid bot token
    When I query resource "channel_members" with channel "C001"
    Then the result has records

  Scenario: Get thread replies
    Given a Slack connector with valid bot token
    When I query resource "thread_replies" with channel "C001"
    Then the result is an error

  Scenario: Post a thread reply
    Given a Slack connector with valid bot token
    When I write resource "thread_reply" with channel "C001" and thread_ts "123456.000001" and text "A reply"
    Then the write succeeds

  Scenario: Get user presence status
    Given a Slack connector with valid bot token
    When I query resource "user_presence" with user "U001"
    Then the result has records

  Scenario: Get user profile
    Given a Slack connector with valid bot token
    When I query resource "user_profile" with user "U001"
    Then the result has records

  Scenario: Lookup user by email
    Given a Slack connector with valid bot token
    When I query resource "user_lookup" with email "alice@example.com"
    Then the result has records

  Scenario: Post an ephemeral message
    Given a Slack connector with valid bot token
    When I write resource "ephemeral_message" with channel "C001" and user "U001" and text "Private note"
    Then the write succeeds

  Scenario: Update a message
    Given a Slack connector with valid bot token
    When I write resource "message_update" with channel "C001" and ts "123456.000001" and text "Edited"
    Then the write succeeds

  Scenario: Delete a message
    Given a Slack connector with valid bot token
    When I write resource "message_delete" with channel "C001" and ts "123456.000001"
    Then the write succeeds

  Scenario: Join a channel
    Given a Slack connector with valid bot token
    When I write resource "channel_join" with channel "C002"
    Then the write succeeds

  Scenario: Archive a channel
    Given a Slack connector with valid bot token
    When I write resource "channel_archive" with channel "C002"
    Then the write succeeds

  Scenario: Unarchive a channel
    Given a Slack connector with valid bot token
    When I write resource "channel_unarchive" with channel "C002"
    Then the write succeeds

  Scenario: Ephemeral message without user raises an error
    Given a Slack connector with valid bot token
    When I write resource "ephemeral_message" with channel "C001" but no user
    Then the write is an error

  Scenario: Search messages across channels
    Given a Slack connector with valid bot token
    When I query resource "message_search" with query "deploy failed"
    Then the result has records

  Scenario: Search messages without query raises an error
    Given a Slack connector with valid bot token
    When I query resource "message_search" without a query filter
    Then the result is an error

  Scenario: Schedule a message
    Given a Slack connector with valid bot token
    When I write resource "schedule_message" with channel "C001" and post_at "1610118217"
    Then the write succeeds

  Scenario: Schedule message without post_at raises an error
    Given a Slack connector with valid bot token
    When I write resource "schedule_message" with channel "C001" but no post_at
    Then the write is an error

  Scenario: Upload a file to a channel
    Given a Slack connector with valid bot token
    When I write resource "file_upload" with filename "notes.txt" and content "hello"
    Then the write succeeds

  Scenario: Upload file without content raises an error
    Given a Slack connector with valid bot token
    When I write resource "file_upload" with filename "notes.txt" but no content
    Then the write is an error
