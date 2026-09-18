Feature: Model Backend Health Check
  As a pipeline author
  I want pipeline validation to block creation and execution when a model backend is unhealthy
  So that I do not run pipelines against backends that will fail

  Scenario: Save-time validation blocks on unhealthy backend
    Given a pipeline with a model backend that has a health check error
    When the pipeline graph is validated at save time
    Then a MODEL_BACKEND_UNHEALTHY error is returned
    And the error includes the backend name and health check error detail

  Scenario: Run-time validation blocks on unhealthy backend
    Given a pipeline with a model backend that has a health check error
    When a pipeline run is created
    Then the run is blocked with a MODEL_BACKEND_UNHEALTHY error

  Scenario: Healthy backend passes validation
    Given a pipeline with a model backend that passed its health check
    When the pipeline graph is validated at save time
    Then no MODEL_BACKEND_UNHEALTHY error is returned

  Scenario: Never-checked backend passes validation
    Given a pipeline with a model backend that has never been health-checked
    When the pipeline graph is validated at save time
    Then no MODEL_BACKEND_UNHEALTHY error is returned

  Scenario: Creating a backend records a successful health check on save
    Given a model backend is created with a healthy health check
    When the model backend creation is submitted
    Then the backend health check result is persisted as healthy

  Scenario: Creating a backend with invalid credentials records the health check error
    Given a model backend is created with an unhealthy health check
    When the model backend creation is submitted
    Then the backend health check result is persisted with the error detail

  Scenario: Updating the API key re-runs the health check on save
    Given a model backend API key update with an unhealthy health check
    When the model backend update is submitted
    Then the backend health check result is persisted with the error detail
