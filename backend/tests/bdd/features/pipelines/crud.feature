Feature: Pipeline CRUD
  As a pipeline author
  I want to create, read, update and delete pipelines
  So that I can manage my team's agentic workflows

  Scenario: Create a pipeline
    Given I am authenticated as an admin in org "acme"
    When I POST /api/pipelines with name "my-pipeline" and valid config
    Then the response status is 201
    And the response contains id and slug

  Scenario: List pipelines
    Given org "acme" has pipelines "alpha", "beta", "gamma"
    And I am authenticated in org "acme"
    When I GET /api/pipelines
    Then the response contains 3 pipelines

  Scenario: Get pipeline by id
    Given org "acme" has pipeline "alpha" with id "11111111-2222-3333-4444-555555555555"
    And I am authenticated in org "acme"
    When I GET /api/pipelines/11111111-2222-3333-4444-555555555555
    Then the response status is 200
    And the response name is "alpha"

  Scenario: Update pipeline config
    Given org "acme" has pipeline "alpha"
    And I am authenticated as an admin in org "acme"
    When I PATCH /api/pipelines/alpha with new config
    Then the response status is 200

  Scenario: Delete pipeline
    Given org "acme" has pipeline "obsolete"
    And I am authenticated as an admin in org "acme"
    When I DELETE /api/pipelines/obsolete
    Then the response status is 204
    And the pipeline no longer exists

  Scenario: Create snapshot at run start
    Given org "acme" has pipeline "my-pipeline" with agents and connectors
    When I start a run for pipeline "my-pipeline"
    Then a snapshot is created with version 1
    And the snapshot contains all connector bindings, schema pins, and model backend pins
    And the snapshot graph matches the live pipeline graph

  Scenario: List snapshots with pagination
    Given org "acme" has pipeline "my-pipeline" with 5 snapshots
    When I list snapshots for pipeline "my-pipeline" with page 1 and page_size 2
    Then the response status is 200
    And the response contains 2 snapshots ordered by version descending
    And the response total_count is 5

  Scenario: Get snapshot by id
    Given org "acme" has pipeline "my-pipeline" with snapshot "snap-1"
    When I get snapshot "snap-1" for pipeline "my-pipeline"
    Then the response status is 200
    And the snapshot has full graph detail

  Scenario: Tag a snapshot
    Given org "acme" has pipeline "my-pipeline" with snapshot "snap-1"
    When I tag snapshot "snap-1" with tag "release-1.0" and notes "Initial release"
    Then the response status is 200
    And the snapshot tag is "release-1.0"
    And the snapshot notes are "Initial release"

  Scenario: Rollback to a previous snapshot
    Given org "acme" has pipeline "my-pipeline" with snapshots "snap-1" and "snap-2"
    When I POST /api/pipelines/my-pipeline/rollback to snapshot "snap-1"
    Then a new snapshot is created with tag "rollback-v1"
    And the pipeline graph matches "snap-1"
    And the new snapshot version is 3

  Scenario: Rollback that would weaken a HITL gate is denied
    Given org "acme" has pipeline "my-pipeline" with snapshots "snap-1" and "snap-2"
    And the rollback would weaken a HITL gate for a non-privileged caller
    When I POST /api/pipelines/my-pipeline/rollback to snapshot "snap-1"
    Then the response status is 403
    And the error says "Gate weakening denied"

  Scenario: Cloning a pipeline records an audit event
    Given org "acme" has pipeline "my-pipeline"
    When I clone pipeline "my-pipeline"
    Then the response status is 201
    And a clone audit event is recorded

  Scenario: Delete a historical snapshot
    Given org "acme" has pipeline "my-pipeline" with snapshots "snap-1" and "snap-2"
    When I delete snapshot "snap-1"
    Then the response status is 204
    And snapshot "snap-1" no longer exists

  Scenario: Cannot delete the latest snapshot
    Given org "acme" has pipeline "my-pipeline" with snapshots "snap-1" and "snap-2"
    When I delete snapshot "snap-2"
    Then the response status is 409
    And the error says "Cannot delete the latest snapshot"

  Scenario: Diff two snapshots
    Given org "acme" has pipeline "my-pipeline" with snapshots "snap-1" and "snap-2"
    When I diff snapshots "snap-1" and "snap-2"
    Then the response status is 200
    And the diff contains added, removed, or modified nodes and edges

  Scenario: Missing pipeline returns 404 for snapshot
    Given I am authenticated in org "acme"
    When I list snapshots for pipeline "non-existent"
    Then the response status is 404

  Scenario: Create snapshot from empty pipeline
    Given org "acme" has pipeline "empty-pipeline" with no agents or connectors
    When I start a run for pipeline "empty-pipeline"
    Then a snapshot is created with version 1
    And the snapshot has an empty graph with no nodes and no edges
