Feature: HITL Gate Policies
  As a reviewer
  I want modify-then-approve, human-only refusal and overdue warnings to hold
  So that reviewers can correct agent output, programmatic credentials stay out
  of human-only gates, and stale claims surface for follow-up

  Background:
    Given a browser reviewer is signed in

  Scenario: Modify-then-approve resumes the run with the reviewer's output
    Given a HITL gate "pre-deploy" is awaiting review
    And the browser reviewer holds the claim
    When the reviewer approves with a modified output
    Then the response status is 200
    And the resume decision carries the modified output

  Scenario: Modify-then-approve without a claim token is rejected
    Given a HITL gate "pre-deploy" is awaiting review
    When the reviewer approves with a modified output without a claim token
    Then the response status is 422

  Scenario: Modify-then-approve with an expired claim token is rejected
    Given a HITL gate "pre-deploy" is awaiting review
    And the browser reviewer holds the claim
    And the claim token has expired
    When the reviewer approves with a modified output
    Then the response status is 410

  Scenario: A non-browser credential cannot decide a human_only gate
    Given a HITL gate "pre-deploy" is awaiting review
    And the gate is a human_only gate
    When an API-key credential approves the gate
    Then the response status is 403

  Scenario: Overdue claims are reported with their age and warning status
    Given a claimed HITL gate has been held for 5 hours
    When the overdue claims are queried
    Then the claim is reported as "warning" about 5 hours old

  Scenario: Overdue claims past the escalation threshold are escalated
    Given a claimed HITL gate has been held for 30 hours
    When the overdue claims are queried
    Then the claim is reported as "escalated"
