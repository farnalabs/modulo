Feature: Contribute fixtures to the community library
  Users can create draft fixture contributions, submit them for review,
  and publish them to the community library.

  Background:
    Given the organisation exists
    And the user is authenticated

  Scenario: Create a draft fixture contribution
    When the user sends POST /api/v1/library/contribute
      | name       | "My Test Fixture" |
      | slug       | "my-test-fixture" |
      | fixture_map | {"prompt": "response"} |
    Then the response status is 201
    And the response has contribution_status "draft"

  Scenario: Create contribution with missing required fields returns 422
    When the user sends POST /api/v1/library/contribute
      | name | "Incomplete" |
    Then the response status is 422

  Scenario: Submit a draft for review
    Given a draft fixture contribution exists
    When the user sends POST /api/v1/library/contribute/{primitive_id}/submit
    Then the response status is 200
    And the response has contribution_status "review_queue"

  Scenario: Submit a non-draft contribution returns 409
    Given a published fixture contribution exists
    When the user sends POST /api/v1/library/contribute/{primitive_id}/submit
    Then the response status is 409

  Scenario: Publish a reviewed contribution
    Given a reviewed fixture contribution exists
    And the user is an org admin
    When the user sends POST /api/v1/library/contribute/{primitive_id}/publish
    Then the response status is 200
    And the response has contribution_status "published"
    And the response has visibility "community"

  Scenario: Publish without admin role returns 403
    Given a reviewed fixture contribution exists
    And the user is a viewer
    When the user sends POST /api/v1/library/contribute/{primitive_id}/publish
    Then the response status is 403

  Scenario: List contributions
    When the user requests GET /api/v1/library/contribute
    Then the response contains a list of contributions

  Scenario: List contributions filtered by status
    When the user requests GET /api/v1/library/contribute?contribution_status=draft
    Then the response contains only draft contributions

  Scenario: Submit a new version
    Given a published fixture contribution exists
    When the user sends POST /api/v1/library/contribute/{primitive_id}/versions
      | name       | "Updated Fixture" |
      | slug       | "updated-fixture" |
      | fixture_map | {"new": "response"} |
    Then the response status is 201
    And the new version has contribution_status "draft"

  Scenario: Submit version on draft original returns 409
    Given a draft fixture contribution exists
    When the user sends POST /api/v1/library/contribute/{primitive_id}/versions
      | name       | "Nope" |
      | slug       | "nope" |
      | fixture_map | {"a": "b"} |
    Then the response status is 409

  Scenario: List versions of a contribution
    Given a published fixture contribution with versions exists
    When the user requests GET /api/v1/library/contribute/{primitive_id}/versions
    Then the response contains a list of versions
