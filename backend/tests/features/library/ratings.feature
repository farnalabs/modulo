Feature: Library Primitive Ratings
  As a library user
  I want to rate primitives I have used
  So that the community can benefit from collective quality signals

  Scenario: Submit a thumbs-up rating
    Given a library primitive "prd-input" exists
    And I have previously copied primitive "prd-input"
    When I submit a rating for "prd-input" with thumbs_up true
    Then the response status is 201
    And the rating has thumbs_up = true

  Scenario: Submit a thumbs-down rating with comment
    Given a library primitive "prd-input" exists
    And I have previously copied primitive "prd-input"
    When I submit a rating for "prd-input" with thumbs_up false and comment "Schema missing optional fields"
    Then the response status is 201
    And the rating has thumbs_up = false
    And the rating has comment = "Schema missing optional fields"

  Scenario: Cannot rate a primitive you have not used
    Given a library primitive "prd-input" exists
    And I have not copied primitive "prd-input"
    When I submit a rating for "prd-input" with thumbs_up true
    Then the response status is 403
    And the error indicates you must copy before rating

  Scenario: Cannot self-rate your own primitive
    Given I created a library primitive "my-agent"
    When I submit a rating for "my-agent" with thumbs_up true
    Then the response status is 403
    And the error indicates self-rating is not allowed

  Scenario: View aggregate rating for a primitive
    Given a library primitive "prd-input" exists
    And 3 users have rated it (2 thumbs up, 1 thumbs down)
    When I request the aggregate rating for "prd-input"
    Then the response status is 200
    And the response contains average_rating
    And the response contains review_count = 3

  Scenario: List all ratings for a primitive
    Given a library primitive "prd-input" exists
    And 3 users have rated it (2 thumbs up, 1 thumbs down)
    When I request the list of ratings for "prd-input"
    Then the response status is 200
    And the response contains a list of ratings
    And each rating has id, thumbs_up, comment, and created_at

  Scenario: Update existing rating
    Given a library primitive "prd-input" exists
    And I previously rated "prd-input" with thumbs_up false
    When I update my rating for "prd-input" with thumbs_up true
    Then the response status is 200
    And the rating has thumbs_up = true

  Scenario: Rating cooldown is enforced
    Given a library primitive "prd-input" exists
    And I have previously copied primitive "prd-input"
    And I submitted a rating 5 minutes ago
    When I submit a rating for "prd-input" with thumbs_up true
    Then the response status is 429
    And the error indicates a 10-minute cooldown between ratings

  Scenario: Primitive with no ratings returns zero counts
    Given a library primitive "unrated-primitive" exists
    When I request the aggregate rating for "unrated-primitive"
    Then the response status is 200
    And the response contains average_rating = null
    And the response contains review_count = 0

