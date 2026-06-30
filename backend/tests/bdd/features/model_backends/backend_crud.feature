Feature: Model Backend CRUD
  As a platform operator
  I want to manage model backends via the REST API
  So that pipelines can use configured AI providers

  Scenario: Create a model backend with valid data
    Given a valid model backend payload for provider "openai"
    When I POST /api/v1/model-backends
    Then the model backend response status is 201
    And the response contains the created model backend
    And the response has_credentials is true
    And the API key is not exposed in the response

  Scenario: List model backends returns backends in the org
    Given model backends exist for provider "openai" and "anthropic"
    When I GET /api/v1/model-backends
    Then the model backend response status is 200
    And the response contains a list of model backends

  Scenario: Get a specific model backend by ID
    Given a model backend exists for provider "anthropic"
    When I GET /api/v1/model-backends/{backend_id}
    Then the model backend response status is 200
    And the response matches the backend details

  Scenario: Update a model backend name and model ID
    Given a model backend exists for provider "openai"
    When I PATCH /api/v1/model-backends/{backend_id} with a new name and model
    Then the model backend response status is 200
    And the response reflects the updated values

  Scenario: Update a model backend API key
    Given a model backend exists for provider "openai"
    When I PATCH /api/v1/model-backends/{backend_id} with a new API key
    Then the model backend response status is 200
    And the response has_credentials is true
    And the API key is not exposed in the response

  Scenario: Delete a model backend
    Given a model backend exists for provider "anthropic"
    When I DELETE /api/v1/model-backends/{backend_id}
    Then the model backend response status is 204

  Scenario: Get non-existent backend returns 404
    Given a non-existent backend ID
    When I GET /api/v1/model-backends/{backend_id}
    Then the model backend response status is 404

  Scenario: Delete non-existent backend returns 404
    Given a non-existent backend ID
    When I DELETE /api/v1/model-backends/{backend_id}
    Then the model backend response status is 404

  Scenario: Create backend with duplicate name returns error
    Given a model backend exists for provider "openai" with name "my-backend"
    When I POST /api/v1/model-backends with the same name "my-backend"
    Then the model backend response status is 409

  Scenario: Create backend with invalid provider returns error
    Given a valid model backend payload for provider "invalid_provider"
    When I POST /api/v1/model-backends
    Then the model backend response status is 422

  Scenario: Create backend with missing required fields returns error
    Given a model backend payload with missing name
    When I POST /api/v1/model-backends
    Then the model backend response status is 422
