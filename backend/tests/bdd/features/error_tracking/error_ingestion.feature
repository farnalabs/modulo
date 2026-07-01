Feature: Error Ingestion
  As a platform user
  I want errors to be captured and deduplicated
  So that I can monitor platform health

  Background:
    Given an authenticated organisation

  Scenario: Backend error is captured
    When a 500 error occurs on an API endpoint
    Then an error event is created with level "error" and source "backend"
    And an error group is created

  Scenario: Error ingested via API
    When I POST /api/v1/errors/ingest with a valid error event
    Then the response status is 201
    And the response contains a group_id

  Scenario: Duplicate errors are deduplicated
    When I POST the same error event twice
    Then the response contains is_new: true for the first
    And the response contains is_new: false for the second

  Scenario: Invalid error is rejected
    When I POST /api/v1/errors/ingest with an empty message
    Then the response status is 422

  Scenario: Batch ingestion accepts 5 events
    When I POST /api/v1/errors/ingest with 5 error events
    Then the response status is 201
    And the response contains 5 results
