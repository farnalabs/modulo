Feature: Microsoft Teams Connector
  As a pipeline author
  I want to query Microsoft Teams teams, channels, messages, members, users, and groups
  So that my agents can collaborate and communicate via Microsoft Graph API

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates token
    Given a Microsoft Teams connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given a Microsoft Teams connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists teams
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries teams
    Then the result contains Microsoft Teams teams

  Scenario: Connector gets a team by ID
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries team with team_id "team-123"
    Then the result contains the Microsoft Teams team

  Scenario: Connector lists channels for a team
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries channels for team "team-123"
    Then the result contains Microsoft Teams channels

  Scenario: Connector gets a channel by ID
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries channel with team_id "team-123" and channel_id "channel-456"
    Then the result contains the Microsoft Teams channel

  Scenario: Connector lists messages in a channel
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries messages in team "team-123" and channel "channel-456"
    Then the result contains Microsoft Teams messages

  Scenario: Connector lists members of a team
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries members of team "team-123"
    Then the result contains Microsoft Teams members

  Scenario: Connector lists users
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries users
    Then the result contains Microsoft Graph users

  Scenario: Connector lists groups
    Given a Microsoft Teams connector configured with valid credentials
    When the connector queries groups
    Then the result contains Microsoft Graph groups

  Scenario: Connector sends a message to a channel
    Given a Microsoft Teams connector configured with valid credentials
    When the connector sends a message "Hello from Modulo" to team "team-123" and channel "channel-456"
    Then the message is sent successfully

  Scenario: Connector creates a new channel
    Given a Microsoft Teams connector configured with valid credentials
    When the connector creates a channel "Announcements" in team "team-123"
    Then the channel is created successfully
