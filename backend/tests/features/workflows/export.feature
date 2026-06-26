Feature: Export workflow as bundle
  Users can export a pipeline as a portable .modulo.zip bundle.
  The bundle strips org-private data like owner_team_id and credentials.

  Background:
    Given a pipeline exists with 2 agent nodes and 1 manual node
    And the pipeline has 3 edges connecting the nodes
    And each agent references a schema and a model backend

  Scenario: Export a pipeline as ZIP
    When the user sends POST /api/v1/libraries/export/{pipeline_id}
    Then the response status is 200
    And the response has content-type "application/zip"
    And the response has a Content-Disposition header with filename ending in ".modulo.zip"

  Scenario: Exported bundle contains bundle.json
    When the exported ZIP is extracted
    Then the bundle.json file exists in the archive root
    And bundle.json contains "format_version", "pipeline", "agents", "schemas", "edges"

  Scenario: Exported bundle strips owner_team_id
    When the bundle.json is inspected
    Then the pipeline section does not contain owner_team_id
    And the pipeline section contains the pipeline name and graph nodes

  Scenario: Exported bundle includes agent definitions
    When the bundle.json agents array is inspected
    Then each agent has name, prompt_template, schema references, and model_backend_id
    And agent definitions do not include credentials or ciphertexts

  Scenario: Export a non-existent pipeline returns 404
    When the user sends POST /api/v1/libraries/export/00000000-0000-0000-0000-000000099999
    Then the response status is 404
