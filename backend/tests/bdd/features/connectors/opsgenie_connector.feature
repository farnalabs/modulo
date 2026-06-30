Feature: Opsgenie Connector
  As a pipeline author
  I want to interact with Opsgenie via the connector
  So that I can manage alerts, teams, schedules, on-calls, and escalations

  Scenario: List alerts returns results
    Given an Opsgenie connector with valid API key
    When I query resource "alerts" with limit 10
    Then the result has records
    And the records contain alert metadata

  Scenario: List alerts with status filter
    Given an Opsgenie connector with valid API key
    When I query resource "alerts" with status "open"
    Then the result has records

  Scenario: List alerts with pagination cursor
    Given an Opsgenie connector with valid API key
    When I query resource "alerts" with cursor "10"
    Then the result has records

  Scenario: Get single alert by ID
    Given an Opsgenie connector with valid API key
    When I query resource "alert" with identifierType "id" and id "abc-123"
    Then the result has records

  Scenario: Get alert notes
    Given an Opsgenie connector with valid API key
    When I query resource "alert_notes" with id "abc-123"
    Then the result has records

  Scenario: Get alert logs
    Given an Opsgenie connector with valid API key
    When I query resource "alert_logs" with id "abc-123"
    Then the result has records

  Scenario: List teams
    Given an Opsgenie connector with valid API key
    When I query resource "teams" with limit 10
    Then the result has records

  Scenario: List schedules
    Given an Opsgenie connector with valid API key
    When I query resource "schedules" with limit 10
    Then the result has records

  Scenario: Get on-calls for a schedule
    Given an Opsgenie connector with valid API key
    When I query resource "on_calls" with schedule_id "sch-456"
    Then the result has records

  Scenario: List escalations
    Given an Opsgenie connector with valid API key
    When I query resource "escalations" with limit 10
    Then the result has records

  Scenario: Create an alert
    Given an Opsgenie connector with valid API key
    When I write resource "alert" with message "Production down"
    Then the write succeeds

  Scenario: Acknowledge an alert
    Given an Opsgenie connector with valid API key
    When I write resource "alert_acknowledge" with id "alert-789"
    Then the write succeeds

  Scenario: Close an alert
    Given an Opsgenie connector with valid API key
    When I write resource "alert_close" with id "alert-789"
    Then the write succeeds

  Scenario: Add a note to an alert
    Given an Opsgenie connector with valid API key
    When I write resource "alert_note" with id "alert-789" and note "Investigating"
    Then the write succeeds

  Scenario: Snooze an alert
    Given an Opsgenie connector with valid API key
    When I write resource "alert_snooze" with id "alert-789" and end_time "2026-07-01T00:00:00Z"
    Then the write succeeds

  Scenario: Health check succeeds with valid API key
    Given an Opsgenie connector with valid API key
    When I check the connector health
    Then the health check reports "ok"
