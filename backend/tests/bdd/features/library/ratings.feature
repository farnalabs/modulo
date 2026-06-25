Feature: Rate community primitives
  Users can rate primitives with thumbs up/down and optional comments,
  and see aggregate rating displays on primitives.

  Background:
    Given a community primitive "PRD Input Schema" exists
    And 3 users have rated it (2 thumbs up, 1 thumbs down)

  Scenario: View rating aggregate
    When the user requests GET /api/v1/libraries/{primitive_id}/ratings/aggregate
    Then the response contains average_rating
    And the response contains review_count = 3

  Scenario: Submit a thumbs-up rating
    When the user sends POST /api/v1/libraries/{primitive_id}/ratings
      | thumbs_up | true |
      | comment   | "Great schema!" |
    Then the response status is 201
    And the rating has thumbs_up = true
    And the rating has comment = "Great schema!"

  Scenario: Submit a thumbs-down rating without comment
    When the user sends POST /api/v1/libraries/{primitive_id}/ratings
      | thumbs_up | false |
    Then the response status is 201
    And the rating has thumbs_up = false
    And the rating has comment = null

  Scenario: List ratings for a primitive
    When the user requests GET /api/v1/libraries/{primitive_id}/ratings
    Then the response contains a list of ratings
    And each rating has id, thumbs_up, comment, and created_at

  Scenario: Rating aggregate updates after new rating
    Given the primitive has 3 ratings with average 3.33
    When a user submits a thumbs-up rating
    Then the aggregate average_rating increases
    And review_count becomes 4
