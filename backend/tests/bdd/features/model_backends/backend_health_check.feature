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
