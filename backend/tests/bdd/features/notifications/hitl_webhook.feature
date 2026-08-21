Feature: HITL Webhook Notification
  As a pipeline operator
  I want to receive webhook notifications when a run reaches a HITL gate
  So that approvers are notified immediately

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Webhook is sent when HITL gate is reached
    Given pipeline "deploy-service" has an approval gate at node "pre-deploy"
    And the pipeline has HITL webhook configured at "https://hooks.example.com/hitl"
    When the run reaches the "pre-deploy" node
    Then a webhook POST is sent to "https://hooks.example.com/hitl"
    And the webhook body contains the run_id and gate_id

  Scenario: Webhook payload includes gate context
    Given pipeline "deploy-service" has an approval gate at node "pre-deploy"
    And the pipeline has HITL webhook configured
    When the run reaches the "pre-deploy" node
    Then the webhook payload includes the pipeline name
    And the webhook payload includes the node name

  Scenario: Webhook is retried on failure
    Given pipeline "deploy-service" has an approval gate at node "pre-deploy"
    And the HITL webhook endpoint returns 500
    When the run reaches the "pre-deploy" node
    Then the webhook is retried up to 3 times
    And after 3 failures, the event is logged to the dead-letter queue

  Scenario: Webhook notification respects org scoping
    Given pipeline "deploy-service" has an approval gate at node "pre-deploy"
    And the pipeline has HITL webhook configured
    When the run reaches the "pre-deploy" node
    Then the webhook is signed with the org's webhook secret
