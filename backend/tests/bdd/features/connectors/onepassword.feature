Feature: 1Password Connect Connector
  As a secrets manager
  I want to interact with 1Password Connect via the connector
  So that I can manage vault items, secrets, and files

  Scenario: Health check returns ok
    Given a 1Password connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Query vaults returns results
    Given a 1Password connector with valid token
    When I query 1Password resource "vaults"
    Then the result has records

  Scenario: Query vault returns a single vault
    Given a 1Password connector with valid token
    When I query 1Password resource "vault" with vault_id "abc123"
    Then the result has records

  Scenario: Query items returns results
    Given a 1Password connector with valid token
    When I query 1Password resource "items" with vault_id "vault1"
    Then the result has records

  Scenario: Query item returns a single item
    Given a 1Password connector with valid token
    When I query 1Password resource "item" with vault_id "vault1" and item_id "item1"
    Then the result has records

  Scenario: Query item_by_title returns matching items
    Given a 1Password connector with valid token
    When I query 1Password resource "item_by_title" with vault_id "vault1" and title "My Login"
    Then the result has records

  Scenario: Query files for an item
    Given a 1Password connector with valid token
    When I query 1Password resource "files" with vault_id "vault1" and item_id "item1"
    Then the result has records

  Scenario: Query file content
    Given a 1Password connector with valid token
    When I query 1Password resource "file" with vault_id "vault1" item_id "item1" and file_id "f1"
    Then the result has records

  Scenario: Create a new item
    Given a 1Password connector with valid token
    When I write 1Password resource "item" with vault_id "vault1" title "New Item" type "LOGIN"
    Then the write succeeds

  Scenario: Update an existing item
    Given a 1Password connector with valid token
    When I write 1Password resource "item_update" with vault_id "vault1" item_id "item1" title "Updated"
    Then the write succeeds

  Scenario: Delete an item
    Given a 1Password connector with valid token
    When I write 1Password resource "item_delete" with vault_id "vault1" and item_id "item1"
    Then the write succeeds

  Scenario: Archive an item
    Given a 1Password connector with valid token
    When I write 1Password resource "item_archive" with vault_id "vault1" and item_id "item1"
    Then the write succeeds

  Scenario: Query missing vault_id raises error
    Given a 1Password connector with valid token
    When I query 1Password resource "items" without vault_id
    Then the result is an error

  Scenario: Query missing item_id raises error
    Given a 1Password connector with valid token
    When I query 1Password resource "item" with vault_id "vault1" without item_id
    Then the result is an error

  Scenario: Unsupported resource raises an error
    Given a 1Password connector with valid token
    When I query 1Password resource "invalid"
    Then the result is an error
