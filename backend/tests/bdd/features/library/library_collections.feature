Feature: Library collection authoring / install / uninstall / grant lifecycle
  As an organisation operator
  I want to author a library collection with manifest pins, publish it,
  install it into runnable org entities, uninstall it, and grant
  tool/connector access for community-sourced collections
  So that I can adopt proven bundles without hand-wiring every entity

  The authoring/install/uninstall/grant REST surface is served by
  `/api/v1/libraries/collections` (`api/routes/library.py`) over the
  `core/library_service` authoring + install/uninstall/grant machinery
  (FAR-760 / FAR-762 / FAR-764 / ADR 032).

  Background:
    Given the organisation exists
    And an org operator is authenticated

  Scenario: Install a published collection
    Given a published library collection exists
    When the user installs the collection
    Then the response status is 201
    And the install response has status "installed"
    And the install response is runnable

  Scenario: A collection that is not published cannot be installed
    Given a draft library collection exists
    When the user installs the collection
    Then the response status is 400
    And the error mentions "must be published"

  Scenario: Install with an unresolvable pin is refused
    Given a published library collection exists
    And the collection pins do not resolve
    When the user installs the collection
    Then the response status is 422
    And the error mentions "does not resolve"

  Scenario: Installing a collection twice is refused
    Given a published library collection exists
    And the collection is already installed in the organisation
    When the user installs the collection
    Then the response status is 400
    And the error mentions "already installed"

  Scenario: Uninstall deletes unmodified entities
    Given a published library collection exists
    And the collection is installed
    And no installed entity has been modified
    When the user uninstalls the collection
    Then the response status is 200
    And the uninstall response lists deleted entities
    And the uninstall response detaches no entities

  Scenario: Uninstall detaches modified entities instead of deleting them
    Given a published library collection exists
    And the collection is installed
    And one installed schema has been modified
    When the user uninstalls the collection
    Then the response status is 200
    And the uninstall response detaches the modified entity

  Scenario: Uninstalling an unknown install returns 404
    Given a published library collection exists
    And the collection is installed
    When the user uninstalls the collection with an unknown install id
    Then the response status is 404
    And the error mentions "not found"

  Scenario: Grant tool access to community-sourced collection agents
    Given a published library collection exists
    And the collection is installed from the community
    When the user grants tool access to the installed agents
    Then the response status is 200
    And the install response has agents_granted true

  Scenario: Grant tool access to a local collection is refused
    Given a published library collection exists
    And the collection is installed from a local source
    When the user grants tool access to the installed agents
    Then the response status is 400
    And the error mentions "community"

  Scenario: Grant tool access to an unknown install returns 404
    Given a published library collection exists
    And the collection is installed
    And the install is unknown
    When the user grants tool access to the installed agents
    Then the response status is 404
    And the error mentions "not found"

  Scenario: Grant tool access is idempotent once already granted
    Given a published library collection exists
    And the collection is installed from the community
    And the install already has agent access granted
    When the user grants tool access to the installed agents
    Then the response status is 200
    And the install response has agents_granted true
    And the install response has status "installed"

  # ---------------------------------------------------------------------------
  # Authoring (FAR-760): create / update / publish a collection with manifest
  # pins. The create/update/publish routes were previously unit/integration
  # covered only while the flag-gated authoring UI had no behaviour-layer
  # surface; these scenarios drive the real /api/v1/libraries/collections
  # authoring routes with only the DB/flag seams patched.
  # ---------------------------------------------------------------------------

  Scenario: Create a draft collection with manifest pins
    Given the operator authors a collection named "My Collection"
    When the operator creates the collection with pins for "input-schema@1.0"
    Then the response status is 201
    And the collection response has status "draft"
    And the collection response echoes manifest pins for "input-schema@1.0"

  Scenario: Creating a collection with a duplicate slug is refused
    Given the operator authors a collection named "My Collection"
    And a collection with slug "my-collection" already exists
    When the operator creates the collection with pins for "input-schema@1.0"
    Then the response status is 409
    And the error mentions "already exists"

  Scenario: Updating a draft collection replaces its manifest pins
    Given a draft library collection exists
    When the operator updates the collection to pin "output-schema@2.0"
    Then the response status is 200
    And the collection response has status "draft"
    And the collection response echoes manifest pins for "output-schema@2.0"

  Scenario: Updating a published collection is refused
    Given a published library collection exists
    When the operator updates the collection to pin "output-schema@2.0"
    Then the response status is 400
    And the error mentions "cannot mutate"

  Scenario: Publishing a draft collection with valid pins succeeds
    Given a draft library collection exists
    And the collection pins resolve to known primitives
    When the operator publishes the collection
    Then the response status is 200
    And the collection response has status "published"

  Scenario: Publishing a collection with an empty manifest is refused
    Given a draft library collection exists
    And the collection has an empty manifest
    When the operator publishes the collection
    Then the response status is 422
    And the error mentions "must not be empty"

  Scenario: Publishing a collection with duplicate pins is refused
    Given a draft library collection exists
    And the collection pins are duplicated
    When the operator publishes the collection
    Then the response status is 422
    And the error mentions "duplicate pin"

  Scenario: Publishing a collection with more than the pin cap is refused
    Given a draft library collection exists
    And the collection has more than 25 pins
    When the operator publishes the collection
    Then the response status is 422
    And the error mentions "must not exceed"

  Scenario: Publishing a collection that pins an unknown primitive is refused
    Given a draft library collection exists
    When the operator publishes the collection
    Then the response status is 422
    And the error mentions "unknown primitive"

  Scenario: Publishing a non-draft collection is refused
    Given a published library collection exists
    When the operator publishes the collection
    Then the response status is 400
    And the error mentions "cannot mutate"
