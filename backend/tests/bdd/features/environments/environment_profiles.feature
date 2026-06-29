Feature: Environment Profiles
  As a platform engineer
  I want to define reusable environment profiles with specific capabilities
  So that pipeline runs execute in consistent, isolated, and appropriately-scoped workspaces

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create an environment profile
    Given a valid environment profile payload with name "python-dev", image "python:3.12-slim", capabilities ["docker", "gpu"], egress "allow_all", and timeout 7200
    When I POST /api/v1/environments with the profile payload
    Then the response status is 201
    And the response contains a profile with name "python-dev"
    And the profile has image_ref "python:3.12-slim"
    And the profile has capabilities ["docker", "gpu"]
    And the profile has egress_policy "allow_all"
    And the profile has timeout_seconds 7200

  Scenario: Create profile with empty name returns validation error
    Given an invalid environment profile payload with empty name
    When I POST /api/v1/environments with the invalid payload
    Then the response status is 422
    And the error indicates "name" is required

  Scenario: Create profile with out-of-range timeout returns validation error
    Given an invalid environment profile payload with timeout 30 seconds
    When I POST /api/v1/environments with the invalid payload
    Then the response status is 422
    And the error indicates timeout is out of range

  Scenario: Create profile with invalid egress policy returns validation error
    Given an invalid environment profile payload with egress_policy "bogus"
    When I POST /api/v1/environments with the invalid payload
    Then the response status is 422
    And the error indicates "egress_policy" has an invalid value

  Scenario: List environment profiles
    Given org "acme" has 3 environment profiles
    When I GET /api/v1/environments
    Then the response status is 200
    And the response is a paginated list with 3 items and page_size 20

  Scenario: Get a specific environment profile
    Given org "acme" has an environment profile with id "profile-1"
    When I GET /api/v1/environments/profile-1
    Then the response status is 200
    And the response contains a profile with id "profile-1"

  Scenario: Profile not found returns 404
    Given org "acme" has no environment profile with id "nonexistent"
    When I GET /api/v1/environments/nonexistent
    Then the response status is 404
    And the error message is "Environment profile not found"

  Scenario: Update an environment profile
    Given org "acme" has an environment profile with id "profile-1"
    When I PATCH /api/v1/environments/profile-1 with name "updated-name"
    Then the response status is 200
    And the response contains a profile with name "updated-name"

  Scenario: Update nonexistent profile returns 404
    When I PATCH /api/v1/environments/nonexistent with name "nope"
    Then the response status is 404
    And the error message is "Environment profile not found"

  Scenario: Delete an environment profile
    Given org "acme" has an environment profile with id "profile-1"
    When I DELETE /api/v1/environments/profile-1
    Then the response status is 204

  Scenario: Delete nonexistent profile returns 404
    When I DELETE /api/v1/environments/nonexistent
    Then the response status is 404
    And the error message is "Environment profile not found"

  Scenario: Test an environment profile
    Given org "acme" has an environment profile with id "profile-1"
    When I POST /api/v1/environments/profile-1/test
    Then the response status is 200
    And the response is a Server-Sent Events stream
    And the stream contains a "provisioning" event
    And the stream contains a "command_complete" event
    And the stream contains a "destroyed" event

  Scenario: Test nonexistent profile returns 404
    When I POST /api/v1/environments/nonexistent/test
    Then the response status is 404
    And the error message is "Environment profile not found"

  Scenario: RuntimeProviderHub resolves to local by default
    Given a RuntimeProviderHub with "local" and "e2b" providers registered
    And an environment profile with capabilities ["docker"] and no provider_hint
    When I resolve the profile against the hub
    Then the resolved provider is "local"

  Scenario: RuntimeProviderHub resolves to e2b when profile has provider_hint
    Given a RuntimeProviderHub with "local" and "e2b" providers registered
    And an environment profile with provider_hint "e2b"
    When I resolve the profile against the hub
    Then the resolved provider is "e2b"

  Scenario: WorkspaceLease lifecycle
    Given a run with id "run-1"
    And a WorkspaceLease for run "run-1" referencing environment profile "profile-1"
    When the run starts executing
    Then the WorkspaceLease status transitions from "pending" to "provisioning"
    When the workspace is created
    Then the WorkspaceLease status transitions from "provisioning" to "active"
    And the lease has a provider_ref and expires_at set
    When the run completes
    Then the WorkspaceLease status is "completed"

  Scenario: Workspace creation via RuntimeProvider
    Given a LocalRuntimeProvider
    And an EnvironmentProfile with image_ref "python:3.12-slim" and capabilities ["docker"]
    When I call create_workspace with a WorkspaceSpec derived from the profile
    Then a provider_ref is returned
    And the workspace status is "running"

  Scenario: Workspace destruction via RuntimeProvider
    Given a LocalRuntimeProvider
    And an EnvironmentProfile with image_ref "python:3.12-slim" and capabilities ["docker"]
    When I call create_workspace with a WorkspaceSpec derived from the profile
    And I call destroy_workspace with the provider_ref
    Then the workspace status is "terminated"

  Scenario: ShellConnector executes commands via RuntimeProvider
    Given a LocalRuntimeProvider with an active workspace
    And a ShellConnector using that provider
    When I execute the command "echo hello" via the ShellConnector
    Then the command exits with code 0
    And the stdout contains "hello"

  Scenario: Validation fails when profile lacks required capabilities
    Given an EnvironmentProfile with capabilities ["docker", "python3.12"]
    And a pipeline snapshot with an agent that requires capabilities ["docker", "python3.12", "egress:github.com"]
    When I validate the snapshot against the profile
    Then a validation error is raised with code "ENV_MISSING_CAPABILITIES"
    And the error mentions "egress:github.com" as missing

  Scenario: Validation passes when profile covers all required capabilities
    Given an EnvironmentProfile with capabilities ["docker", "python3.12", "egress:github.com"]
    And a pipeline snapshot with an agent that requires capabilities ["docker", "python3.12"]
    When I validate the snapshot against the profile
    Then no validation errors are raised

  Scenario: Cross-org isolation blocks access to another org's profile
    Given org "acme" has an environment profile with id "profile-1"
    When I authenticate as a user in org "megacorp"
    And I GET /api/v1/environments/profile-1
    Then the response status is 404
    And the error message is "Environment profile not found"

  Scenario: Cross-org isolation hides profiles from listing
    Given org "acme" has 3 environment profiles
    When I authenticate as a user in org "megacorp"
    And I GET /api/v1/environments
    Then the response contains 0 environment profiles
