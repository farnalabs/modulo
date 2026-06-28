Feature: Eval Suite CRUD
  As a pipeline author
  I want to create, read, update, and delete eval definitions
  So that I can manage quality criteria for my pipelines

  Scenario: Admin creates an eval definition
    Given I am authenticated as an admin in org "acme"
    When I POST /api/evals with name "quality-check" and type "regex"
    Then the response status is 201
    And the response has name "quality-check"

  Scenario: Non-admin cannot create an eval definition
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/evals with name "quality-check" and type "regex"
    Then the response status is 403

  Scenario: List eval definitions returns paginated results
    Given I am authenticated as an admin in org "acme"
    And an eval definition "quality-check" exists
    When I GET /api/evals
    Then the response status is 200
    And the response contains eval definition "quality-check"

  Scenario: Admin updates an eval definition
    Given I am authenticated as an admin in org "acme"
    And an eval definition "quality-check" exists
    When I PUT /api/evals/{eval_id} with a new name "improved-check"
    Then the response status is 200
    And the response has name "improved-check"

  Scenario: Admin deletes an eval definition
    Given I am authenticated as an admin in org "acme"
    And an eval definition "quality-check" exists
    When I DELETE /api/evals/{eval_id}
    Then the response status is 204
