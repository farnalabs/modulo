Feature: Library collection install / uninstall / grant lifecycle
  As an organisation operator
  I want to install a published library collection into runnable org
  entities, uninstall it, and grant tool/connector access for
  community-sourced collections
  So that I can adopt proven bundles without hand-wiring every entity

  The install/uninstall/grant REST surface is served by
  `/api/v1/libraries/collections` (`api/routes/library.py`) over the
  `core/library_service` install/uninstall/grant machinery (FAR-762 /
  FAR-764 / ADR 032).

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