Feature: Feedback Review Inbox and Eval Proposals
  As a pipeline author
  I want a review queue and an eval proposal queue for human feedback on pipeline output
  So that I can triage rejected output, spawn correction runs, and grow the eval suite

  The `/api/v1/feedback/inbox` and `/api/v1/feedback/proposals` surfaces (PRD
  §8.20) host the review workflow: `GET /feedback/inbox` and
  `GET /feedback/inbox/{id}` expose a paginated, filterable review queue;
  `POST /feedback/inbox/{id}/review` advances the workflow with one of three
  actions (`mark_reviewed` resolves the record, `dismiss` discards it,
  `create_correction_run` spawns a correction run and moves the record to
  `correcting`); `POST /feedback/{id}/detect-gap` reports whether a covering
  eval exists; and `GET /feedback/proposals` lists the eval proposals queue so
  a human can publish a proposal (`POST /feedback/proposals/{id}/publish`) as a
  live, pipeline/node-scoped `EvalDefinition`.

  Scenario: The review inbox lists pending human feedback
    Given I am an admin feedback reviewer
    And the organisation has feedback records awaiting review
    When I request GET /api/v1/feedback/inbox
    Then the response status is 200
    And the inbox response contains the feedback record with its pipeline name

  Scenario: The review inbox filters by handler type and status
    Given I am an admin feedback reviewer
    And the organisation has feedback records awaiting review
    When I request GET /api/v1/feedback/inbox?type=human&status=pending
    Then the response status is 200
    And the inbox filter is passed to the feedback manager

  Scenario: An inbox item exposes its detailed record
    Given I am an admin feedback reviewer
    And a feedback record with id "rec-1" is in the inbox
    When I request GET /api/v1/feedback/inbox/rec-1
    Then the response status is 200
    And the response record id matches "rec-1"
    And the response record carries the rejected output

  Scenario: Reviewing with mark_reviewed resolves the record
    Given I am an admin feedback reviewer
    And a feedback record with id "rec-1" is in the inbox
    When I review the feedback record "rec-1" with action "mark_reviewed"
    Then the response status is 200
    And the reviewed record status is "resolved"

  Scenario: Reviewing with dismiss discards the record
    Given I am an admin feedback reviewer
    And a feedback record with id "rec-1" is in the inbox
    When I review the feedback record "rec-1" with action "dismiss"
    Then the response status is 200
    And the reviewed record status is "dismissed"

  Scenario: Reviewing with create_correction_run spawns a correction run
    Given I am an admin feedback reviewer
    And a feedback record with id "rec-1" is in the inbox
    When I review the feedback record "rec-1" with action "create_correction_run"
    Then the response status is 200
    And the review response carries a correction run id
    And the correction run was spawned for the record

  Scenario: Review with an invalid action is rejected
    Given I am an admin feedback reviewer
    And a feedback record with id "rec-1" is in the inbox
    When I review the feedback record "rec-1" with action "yell_at_pipeline"
    Then the response status is 422

  Scenario: Review of a missing record returns 404
    Given I am an admin feedback reviewer
    And no feedback record exists
    When I review the feedback record "missing-rec" with action "mark_reviewed"
    Then the response status is 404

  Scenario: Eval gap detection flags a record with no covering eval
    Given I am an admin feedback reviewer
    And a feedback record with id "rec-1" is in the inbox
    When I run eval-gap detection on the feedback record "rec-1"
    Then the response status is 200
    And the detection response reports eval_gap true

  Scenario: The proposals queue lists eval-gap records
    Given I am an admin feedback reviewer
    And the organisation has an eval proposal record
    When I request GET /api/v1/feedback/proposals
    Then the response status is 200
    And the proposals response contains the proposal record

  Scenario: An eval-gap proposal publishes as a live eval definition
    Given I am an admin feedback reviewer
    And the organisation has an eval proposal record
    When I publish the proposal "rec-1" as eval "answer-must-mention-42"
    Then the response status is 201
    And the published eval is scoped to the record's pipeline and node
    And the published eval has type "regex"

  Scenario: A non-eval-gap record cannot be published as a proposal
    Given I am an admin feedback reviewer
    And the organisation has a non-gap feedback record
    When I publish the proposal "rec-1" as eval "answer-must-mention-42"
    Then the response status is 422

  Scenario: A resolved record cannot be published as a proposal
    Given I am an admin feedback reviewer
    And the organisation has a resolved feedback record
    When I publish the proposal "rec-1" as eval "answer-must-mention-42"
    Then the response status is 409

  Scenario: Publishing a missing proposal returns 404
    Given I am an admin feedback reviewer
    And no feedback record exists
    When I publish the proposal "missing-rec" as eval "answer-must-mention-42"
    Then the response status is 404
