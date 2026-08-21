Feature: PyPI Connector
  As a pipeline author
  I want to interact with the PyPI public registry via the connector
  So that I can query package metadata, search packages, and list package files

  Scenario: Health check returns ok
    Given a PyPI connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with unreachable registry
    Given a PyPI connector with valid token
    And the PyPI registry is unreachable
    When I perform a health check
    Then the health result is not ok

  Scenario: Query package metadata returns results
    Given a PyPI connector with valid token
    When I query PyPI resource "package" with package "requests"
    Then the result has records

  Scenario: Query package version returns detail
    Given a PyPI connector with valid token
    When I query PyPI resource "package_version" with package "requests" and version "2.31.0"
    Then the result has records

  Scenario: Query search returns results
    Given a PyPI connector with valid token
    When I query PyPI resource "search" with text "asyncio" and limit 10
    Then the result has records

  Scenario: Query package files returns files
    Given a PyPI connector with valid token
    When I query PyPI resource "package_files" with package "requests" and version "2.31.0"
    Then the result has records

  Scenario: Query simple list returns versions
    Given a PyPI connector with valid token
    When I query PyPI resource "simple_list" with package "requests"
    Then the result has records

  Scenario: Write raises an error
    Given a PyPI connector with valid token
    When I write to PyPI resource "package"
    Then the write is an error

  Scenario: Missing package filter for package query raises an error
    Given a PyPI connector with valid token
    When I query PyPI resource "package" without package filter
    Then the result is an error

  Scenario: Missing version filter for package_version query raises an error
    Given a PyPI connector with valid token
    When I query PyPI resource "package_version" without version filter
    Then the result is an error

  Scenario: Missing text filter for search raises an error
    Given a PyPI connector with valid token
    When I query PyPI resource "search" without text filter
    Then the result is an error
