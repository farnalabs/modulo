Feature: Slack App-Mention Triggers
  As a pipeline operator
  I want @-mentions of the Modulo Slack app to fire pipeline runs
  So that teams can trigger workflows from Slack chat

  Slack Events API ``app_mention`` deliveries are authenticated with Slack's
  signed-request scheme (``X-Slack-Signature`` HMAC-SHA256 over
  ``v0:<timestamp>:<body>`` with the trigger signing secret) and bounded to a
  ±300s replay window via ``X-Slack-Request-Timestamp``. Duplicate Slack
  ``event_id`` deliveries are deduplicated, concurrency and pipeline rate
  limits are enforced, and every delivery attempt is recorded to the immutable
  TriggerEvent log.

  Scenario: A valid signed app_mention fires a pipeline run
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And Slack delivers an app_mention event with event_id "Ev1234567890"
    When the app_mention delivery is verified and processed
    Then the slack delivery creates a run with trigger_type "slack_app_mention"
    And the slack delivery records a TriggerEvent with result "accepted"
    And the run input_payload carries the event_id "Ev1234567890" and channel "C12345"

  Scenario: Payload mapping remaps mention fields into pipeline input
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret" and payload mapping "text->text, channel->channel"
    And Slack delivers an app_mention event mentioning "<@U42> please run this job"
    When the app_mention delivery is verified and processed
    Then the slack delivery creates a run with trigger_type "slack_app_mention"
    And the run input_payload equals {"text": "<@U42> please run this job", "channel": "C12345"}

  Scenario: url_verification handshake echoes the challenge
    Given Slack sends a url_verification payload with challenge "3eZbrw1aBm2rZgRNFdxV2598559m"
    Then the challenge "3eZbrw1aBm2rZgRNFdxV2598559m" is echoed back

  Scenario: A request signed with the wrong secret is refused and audited
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And Slack signs an app_mention delivery with the wrong secret
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses with a signature error
    And the slack delivery records a TriggerEvent with result "hmac_failed"

  Scenario: A request with an expired timestamp is refused
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And Slack delivers an app_mention event with an expired X-Slack-Request-Timestamp
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses with a timestamp error

  Scenario: A duplicate Slack event_id is deduplicated
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And a Slack app_mention delivery with event_id "Ev-dup" was already processed
    And Slack delivers an app_mention event with event_id "Ev-dup"
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses as a duplicate and no run is created
    And the slack delivery records a TriggerEvent with result "deduplicated"

  Scenario: A non app_mention event is refused and audited
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And Slack delivers a "message" event instead of an app_mention
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses with a payload type error
    And the slack delivery records a TriggerEvent with result "event_type_not_accepted"

  Scenario: A malformed mention payload is refused and audited
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And Slack delivers an app_mention event missing its event_id
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses with a parse error
    And the slack delivery records a TriggerEvent with result "parse_failed"

  Scenario: At the concurrency limit the delivery still queues
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret" and max_concurrent_runs 1
    And Slack delivers an app_mention event while 1 run is already active
    When the app_mention delivery is verified and processed
    Then the slack delivery creates a run with trigger_type "slack_app_mention"
    And the slack delivery records a TriggerEvent with result "accepted"
    And the slack delivery records a TriggerEvent with result "concurrency_limit_reached"

  Scenario: A rate-limited pipeline refuses the delivery
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And the pipeline rate limit is "1 per 3600 seconds" and 1 delivery already fired
    And Slack delivers an app_mention event with event_id "Ev-rate"
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses with a rate limit error
    And the slack delivery records a TriggerEvent with result "rate_limited"

  Scenario: A delivery rejected by the advisory lock is refused as busy
    Given a Slack app_mention trigger with signing secret "my-slack-signing-secret"
    And another delivery is already holding the trigger advisory lock
    And Slack delivers an app_mention event with event_id "Ev-busy"
    When the app_mention delivery is verified and processed
    Then the slack delivery refuses as busy
