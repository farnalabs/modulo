Feature: User Roles
  As an organisation admin
  I want to assign roles to users with different permission levels
  So that access to operations is controlled

  Background:
    Given I am authenticated as an admin in org "acme"

  # ADR 017 per-scenario flip table (PR B):
  # - "Viewer cannot create pipelines" and "Viewer cannot delete pipelines"
  #   flip from 200/204 to 403 once the pipeline sweep (pipeline.create and
  #   pipeline.delete @ operator) lands. The editor role is the operator role.
  # - "Editor can update pipelines" becomes "Operator can update pipelines".

  Scenario: Admin can create pipelines
    When I POST /api/v1/pipelines with name "admin-pipeline" and valid config
    Then the response status is 201

  Scenario: Viewer cannot create pipelines
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/v1/pipelines with name "viewer-pipeline" and valid config
    Then the response status is 403

  Scenario: Viewer can list pipelines
    Given org "acme" has pipeline "my-pipeline"
    And I am authenticated as a viewer in org "acme"
    When I GET /api/v1/pipelines
    Then the response contains 1 pipeline

  Scenario: Viewer cannot delete pipelines
    Given org "acme" has pipeline "my-pipeline"
    And I am authenticated as a viewer in org "acme"
    When I DELETE /api/v1/pipelines/my-pipeline
    Then the response status is 403

  Scenario: Operator can update pipelines
    Given org "acme" has pipeline "my-pipeline"
    And I am authenticated as an operator in org "acme"
    When I PATCH /api/v1/pipelines/my-pipeline with new config
    Then the response status is 200

  Scenario: Admin can manage users
    When I POST /api/v1/admin/users with email "newuser@example.com" and role "viewer"
    Then the response status is 201
