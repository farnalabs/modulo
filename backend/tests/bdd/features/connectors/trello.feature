Feature: Trello Connector
  As a pipeline author
  I want to interact with Trello boards, lists, and cards
  So that my agents can manage projects on Trello

  Background:
    Given a Trello connector with valid API key and token

  Scenario: Health check with valid credentials
    Given the Trello API returns a valid member profile
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with invalid credentials
    Given the Trello API returns 401 Unauthorized
    When I perform a health check
    Then the health result is not ok

  Scenario: List boards
    Given the Trello API returns available boards
    When I query resource "boards" with limit 10
    Then the result has records
    And the records contain board metadata

  Scenario: List lists on a board
    Given the Trello API returns lists for a board
    When I query resource "lists" with board_id "b1"
    Then the result has records
    And the records contain list metadata

  Scenario: List cards on a board
    Given the Trello API returns cards for a board
    When I query resource "cards" with board_id "b1"
    Then the result has records

  Scenario: Get single card
    Given the Trello API returns a single card
    When I query resource "card" with card_id "c1"
    Then the result has records
    And the record contains card fields

  Scenario: Create a card
    Given the Trello API accepts card creation
    When I write resource "card" with name "New Card" and list_id "l1"
    Then the write succeeds

  Scenario: Add comment to card
    Given the Trello API accepts comments
    When I write resource "comment" for card "c1" with text "Nice work!"
    Then the write succeeds

  Scenario: Query unknown resource returns error
    Given the Trello connector is configured
    When I query resource "invalid"
    Then the result is an error
