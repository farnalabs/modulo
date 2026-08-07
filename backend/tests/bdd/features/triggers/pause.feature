Feature: Org-wide "pause all pipeline triggers" kill-switch

  Scenario: Admin pauses all pipeline triggers for the org
    Given I am authenticated as an admin in org "acme"
    When I PUT /api/v1/admin/orgs/acme/triggers/pause with paused true
    Then the response status is 200
    And the response body pause state is true

  Scenario: Webhook delivered to a paused org is dropped with a paused response
    Given I am authenticated as an admin in org "acme"
    And org "acme" has trigger "00000000-0000-0000-0000-00000000000c" with webhook secret "secret"
    When I POST /api/v1/triggers/00000000-0000-0000-0000-00000000000c/webhook with payload {"action":"opened"} and org pause is paused
    Then the response status is 202
    And the webhook is paused

  Scenario: Non-admin cannot pause triggers
    Given I am authenticated as an admin in org "acme"
    When I PUT /api/v1/admin/orgs/acme/triggers/pause with paused true as a non-admin
    Then the response status is 403

  Scenario: Unpause restores webhook acceptance
    Given I am authenticated as an admin in org "acme"
    And org "acme" has trigger "00000000-0000-0000-0000-00000000000c" with webhook secret "secret"
    When I PUT /api/v1/admin/orgs/acme/triggers/pause with paused false
    Then the response status is 200
    When I POST /api/v1/triggers/00000000-0000-0000-0000-00000000000c/webhook with payload {"action":"opened"} and org pause is active
    Then the response status is 202
    And the webhook is accepted
