Feature: Product Analytics Metrics Ingest
  As a tenant user
  I want to stage curated frontend product analytics events
  So that the daily metrics dump can aggregate opt-in telemetry without leaking raw paths

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Stages a valid event batch and returns 204
    Given the org has product analytics consent level "all"
    When I POST /api/v1/metrics/events with 1 valid event
    Then the response status is 204
    And the event is staged for the daily dump

  Scenario: Stages every event in a multi-event batch
    Given the org has product analytics consent level "all"
    When I POST /api/v1/metrics/events with 5 valid events
    Then the response status is 204
    And 5 events are staged for the daily dump

  Scenario: Does not write events when consent is off
    Given the org has product analytics consent level "off"
    When I POST /api/v1/metrics/events with 1 valid event
    Then the response status is 204
    And no events are staged for the daily dump

  Scenario: Does not write events when the org has no analytics settings
    Given the org has no product analytics settings
    When I POST /api/v1/metrics/events with 1 valid event
    Then the response status is 204
    And no events are staged for the daily dump

  Scenario: Does not write events when the organisation is missing
    Given the organisation does not exist
    When I POST /api/v1/metrics/events with 1 valid event
    Then the response status is 204
    And no events are staged for the daily dump

  Scenario: Skips api_error events once the daily cap is reached
    Given the org has product analytics consent level "all"
    And today's api_error count is at the daily cap
    When I POST /api/v1/metrics/events with 5 api_error events
    Then the response status is 204
    And no events are staged for the daily dump

  Scenario: Stages api_error events while under the daily cap
    Given the org has product analytics consent level "all"
    And today's api_error count is below the daily cap
    When I POST /api/v1/metrics/events with 5 api_error events
    Then the response status is 204
    And 5 events are staged for the daily dump

  Scenario: Sanitises an unmatched route path in an api_error payload
    Given the org has product analytics consent level "all"
    When I POST /api/v1/metrics/events with an api_error event carrying route "/my/raw/path"
    Then the response status is 204
    And the staged payload route is "unknown"

  Scenario: Preserves a registered route template in an api_error payload
    Given the org has product analytics consent level "all"
    When I POST /api/v1/metrics/events with an api_error event carrying route "/api/v1/metrics/events"
    Then the response status is 204
    And the staged payload route is "/api/v1/metrics/events"

  Scenario: Duplicate event_id inserts are silently ignored
    Given the org has product analytics consent level "all"
    And the staging insert rejects duplicate event ids
    When I POST /api/v1/metrics/events with 1 valid event
    Then the response status is 204

  Scenario: A staging database failure still returns 204
    Given the org has product analytics consent level "all"
    And the staging insert fails with a database error
    When I POST /api/v1/metrics/events with 1 valid event
    Then the response status is 204

  Scenario: Rejects an empty event batch
    When I POST an empty event batch
    Then the response status is 422

  Scenario: Rejects a batch larger than the maximum size
    When I POST a batch with 1001 events
    Then the response status is 422

  Scenario: Rejects an unknown event type
    When I POST /api/v1/metrics/events with an event of type "bogus_event"
    Then the response status is 422

  Scenario: Rejects an event missing required fields
    When I POST /api/v1/metrics/events with an event missing its event id
    Then the response status is 422