Feature: npm Connector
  As a pipeline author
  I want to interact with the npm public registry via the connector
  So that I can query package metadata, search packages, and list package files

  Scenario: Health check returns ok
    Given an npm connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with unreachable registry
    Given an npm connector with valid token
    And the npm registry is unreachable
    When I perform a health check
    Then the health result is not ok

  Scenario: Query package metadata returns results
    Given an npm connector with valid token
    When I query npm resource "package" with package "express"
    Then the result has records

  Scenario: Query package version returns detail
    Given an npm connector with valid token
    When I query npm resource "package_version" with package "express" and version "4.18.2"
    Then the result has records

  Scenario: Query search returns results
    Given an npm connector with valid token
    When I query npm resource "search" with text "react" and limit 10
    Then the result has records

  Scenario: Query package files returns files
    Given an npm connector with valid token
    When I query npm resource "package_files" with package "express" and version "4.18.2"
    Then the result has records

  Scenario: Query scope packages returns results
    Given an npm connector with valid token
    When I query npm resource "scope_packages" with scope "@angular"
    Then the result has records

  Scenario: Query search with from offset
    Given an npm connector with valid token
    When I query npm resource "search" with text "react" and from 20
    Then the result has records

  Scenario: Write raises an error
    Given an npm connector with valid token
    When I write to npm resource "package"
    Then the write is an error

  Scenario: Missing package filter for package query raises an error
    Given an npm connector with valid token
    When I query npm resource "package" without package filter
    Then the result is an error

  Scenario: Missing version filter for package_version query raises an error
    Given an npm connector with valid token
    When I query npm resource "package_version" without version filter
    Then the result is an error

  Scenario: Missing text filter for search raises an error
    Given an npm connector with valid token
    When I query npm resource "search" without text filter
    Then the result is an error
