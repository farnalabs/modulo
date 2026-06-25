Feature: Webhook Signing
  As a pipeline operator
  I want outgoing webhooks signed with HMAC-SHA256
  So that the receiver can verify the webhook came from Modulo

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Outgoing webhook has HMAC signature header
    Given pipeline "my-pipeline" has a webhook configured
    And the pipeline has webhook secret "whsec_abc123"
    When a webhook notification is sent
    Then the request includes header "X-Modulo-Signature-256"
    And the signature is a valid HMAC-SHA256 of the payload

  Scenario: Signature uses different secrets per org
    Given org "acme" has webhook secret "whsec_acme"
    And org "othercorp" has webhook secret "whsec_other"
    When a webhook is sent from org "acme"
    Then the signature is computed with "whsec_acme"
    And a webhook from org "othercorp" uses "whsec_other"

  Scenario: Timestamp is included in signature
    Given pipeline "my-pipeline" has a webhook configured
    When a webhook notification is sent
    Then the request includes header "X-Modulo-Timestamp"
    And the timestamp is within 5 minutes of current time

  Scenario: Receiver can verify signature
    Given pipeline "my-pipeline" has webhook secret "whsec_abc123"
    And a webhook payload with signature
    When the receiver verifies the signature
    Then the verification succeeds with the correct secret
    And the verification fails with a different secret
