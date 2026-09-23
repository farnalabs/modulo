Feature: Stripe Purchase Webhook Fulfilment
  As Stripe
  I want purchase events to be signature-verified and fulfilled exactly once
  So that a paid team licence is issued for the authoritative invoice.paid event only

  Background:
    Given the purchase webhook is configured with a webhook secret

  Scenario: A validly signed invoice.paid event dispatches fulfilment
    When Stripe sends a signed invoice.paid event for "bob@acme.com" of "Acme Inc"
    Then the response status is 200
    And fulfilment is dispatched exactly once with event id "evt_inv_paid"
    And the fulfilment carries customer email "bob@acme.com"
    And the fulfilment carries org name "Acme Inc"

  Scenario: checkout.session.completed is acknowledged but never fulfils
    When Stripe sends a signed checkout.session.completed event for a paid checkout
    Then the response status is 200
    And no fulfilment is dispatched

  Scenario: checkout followed by invoice.paid fulfils exactly once
    When Stripe sends a signed checkout.session.completed event for a paid checkout
    And Stripe sends a signed invoice.paid event for "bob@acme.com" of "Acme Inc"
    Then the response status is 200
    And fulfilment is dispatched exactly once with event id "evt_inv_paid"

  Scenario: An invoice.paid event without a customer email never dispatches
    When Stripe sends a signed invoice.paid event with no customer email
    Then the response status is 200
    And no fulfilment is dispatched

  Scenario: An unrelated event type is acknowledged without dispatch
    When Stripe sends a signed customer.subscription.updated event
    Then the response status is 200
    And no fulfilment is dispatched

  Scenario: An invalid signature is refused and never fulfils
    When Stripe sends an event with a bogus signature
    Then the response status is 400
    And no fulfilment is dispatched

  Scenario: A tampered payload is refused and never fulfils
    When Stripe sends a tampered invoice.paid event
    Then the response status is 400
    And no fulfilment is dispatched

  Scenario: A stale timestamp outside the replay window is refused
    When Stripe sends a signed event with a stale timestamp
    Then the response status is 400
    And no fulfilment is dispatched

  Scenario: A non-JSON payload is refused
    When Stripe sends a signed non-JSON payload
    Then the response status is 400
    And no fulfilment is dispatched

  Scenario: The webhook is not reachable when Stripe is not configured
    Given the purchase webhook is disabled
    When Stripe sends a signed invoice.paid event for "bob@acme.com" of "Acme Inc"
    Then the response status is 404
    And no fulfilment is dispatched
