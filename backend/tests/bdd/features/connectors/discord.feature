Feature: Discord Connector
  As a pipeline author
  I want to interact with Discord guilds, channels, and messages
  So that my agents can collaborate, send messages, and manage notifications

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates bot token
    Given a Discord connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given a Discord connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists guilds
    Given a Discord connector configured with valid credentials
    When the connector queries guilds
    Then the result contains Discord guilds

  Scenario: Connector lists channels for a guild
    Given a Discord connector configured with valid credentials
    When the connector queries channels for guild "guild-123"
    Then the result contains Discord channels

  Scenario: Connector lists messages in a channel
    Given a Discord connector configured with valid credentials
    When the connector queries messages in channel "channel-456"
    Then the result contains Discord messages

  Scenario: Connector lists guild members
    Given a Discord connector configured with valid credentials
    When the connector queries members of guild "guild-123"
    Then the result contains Discord guild members

  Scenario: Connector lists roles for a guild
    Given a Discord connector configured with valid credentials
    When the connector queries roles for guild "guild-123"
    Then the result contains Discord roles

  Scenario: Connector gets a guild by ID
    Given a Discord connector configured with valid credentials
    When the connector queries guild with guild_id "guild-123"
    Then the result contains the Discord guild

  Scenario: Connector sends a message to a channel
    Given a Discord connector configured with valid credentials
    When the connector sends a message "Hello from Modulo" to channel "channel-456"
    Then the message is sent successfully

  Scenario: Connector adds a reaction to a message
    Given a Discord connector configured with valid credentials
    When the connector adds a reaction "👍" to message "msg-789" in channel "channel-456"
    Then the reaction is added successfully

  Scenario: Connector creates a new text channel
    Given a Discord connector configured with valid credentials
    When the connector creates a channel "announcements" in guild "guild-123"
    Then the channel is created successfully
