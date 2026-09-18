Feature: Quality Report Webhook Delivery
  As an operator
  I want quality report webhook deliveries to be authenticated and configured
  So that recipients can verify the payload authenticity (PRD 8.11)

  Scenario: Delivery signs the payload when a signing secret is configured
    Given a quality report delivery config with a signing secret "s3cret-key"
    When I deliver the quality report
    Then the delivery request is sent as raw JSON bytes
    And the delivery request includes an X-Modulo-Signature header
    And the signature matches an HMAC-SHA256 of the sent bytes computed with "s3cret-key"

  Scenario: Delivery without a signing secret omits the signature header
    Given a quality report delivery config without a signing secret
    When I deliver the quality report
    Then the delivery request is sent via the json argument
    And the delivery request does not include an X-Modulo-Signature header

  Scenario: Delivery uses the configured timeout
    Given a quality report delivery config with a timeout of 5 seconds
    When I deliver the quality report
    Then the delivery client used a timeout of 5 seconds

  Scenario: Cost increase in the weekly trend renders a down arrow
    Given a weekly report where cost increased by 12 percent
    When I format the weekly trend section
    Then the cost line starts with the down arrow

  Scenario: Cost decrease in the weekly trend renders an up arrow
    Given a weekly report where cost decreased by 12 percent
    When I format the weekly trend section
    Then the cost line starts with the up arrow
