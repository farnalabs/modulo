Feature: Quality Report Delivery
  As a pipeline operator
  I want to trigger quality reports and deliver them to Slack webhooks
  So that I receive weekly quality summaries

  Scenario: Trigger quality report (happy path)
    Given I am authenticated as an admin in org "acme"
    And org "acme" has pipeline "quality-report-pipeline"
    And the pipeline has a webhook configured for quality_report events
    When I POST to the quality report endpoint for "quality-report-pipeline"
    Then the response status is 200
    And the response contains period, summary, and deliveries

  Scenario: No notification endpoint configured
    Given I am authenticated as an admin in org "acme"
    And org "acme" has pipeline "no-webhook-pipeline"
    And the pipeline has no notification endpoints
    When I POST to the quality report endpoint for "no-webhook-pipeline"
    Then the response status is 200
    And the response contains empty deliveries

  Scenario: Pipeline not found returns 404
    Given I am authenticated as an admin in org "acme"
    And no pipeline exists for quality report
    When I POST to the quality report endpoint for "nonexistent-pipeline"
    Then the response status is 404
