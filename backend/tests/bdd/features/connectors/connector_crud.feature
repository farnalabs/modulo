Feature: Connector CRUD lifecycle
  As a pipeline operator
  I want to create, retrieve, list, update and delete connector instances through the admin API
  So that I can manage third-party integrations without touching the database

  The `/api/v1/connectors` surface hosts the connector instance lifecycle
  (PRD 7.11): `POST /api/v1/connectors` creates an instance (201), `GET
  /api/v1/connectors` lists and `GET /api/v1/connectors/{id}` fetches an
  instance, `PATCH /api/v1/connectors/{id}` updates an instance (re-encrypting
  fresh credentials and clearing the degraded marker), and `DELETE
  /api/v1/connectors/{id}` removes it (204). Credentials are Fernet-encrypted
  at rest on every write and never echoed in any response — only the boolean
  `has_credentials` is exposed. Org isolation is enforced on every
  read/update/delete: a foreign-org connector resolves to 404 before any
  write. These scenarios drive the real routes with only the DB lookup / CRUD
  / RLS seams patched.

  Background:
    Given I am an admin operator in the organisation

  Scenario: Creating a connector encrypts credentials at rest
    Given the connector store accepts a new connector
    And REST credentials with bearer token "tok-123"
    When I create a connector named "Staging Bot" of type "rest"
    Then the response status is 201
    And the connector response exposes has_credentials true
    And the connector response does not echo the credential plaintext
    And the stored credentials are Fernet-encrypted

  Scenario: Creating a connector rejects malformed REST credentials
    Given REST credentials payload "not-json"
    When I create a connector named "Broken REST" of type "rest"
    Then the response status is 422

  Scenario: Creating a connector rejects an invalid REST config
    Given REST credentials with bearer token "tok-abc"
    And a REST connector config with on_unknown "explode"
    When I create a connector named "Bad Policy" of type "rest"
    Then the response status is 422

  Scenario: A connector is retrievable by id without exposing credentials
    Given a connector with id "conn-1" exists in my organisation
    When I fetch the connector "conn-1"
    Then the response status is 200
    And the connector response exposes has_credentials true
    And the connector response does not echo the credential plaintext

  Scenario: Fetching a connector from another organisation is a 404
    Given a connector with id "conn-1" exists in another organisation
    When I fetch the connector "conn-1"
    Then the response status is 404

  Scenario: Listing connectors is paginated and redacted
    Given my organisation has connectors in the store
    When I list the connectors
    Then the response status is 200
    And the list response reports one connector total
    And the list response does not echo the credential plaintext

  Scenario: Updating a connector re-encrypts fresh credentials
    Given a connector with id "conn-1" exists in my organisation
    When I update connector "conn-1" with fresh REST credentials "tok-456"
    Then the response status is 200
    And the connector response exposes has_credentials true
    And the updated credentials are stored Fernet-encrypted

  Scenario: Deleting a connector removes the instance
    Given a connector with id "conn-1" exists in my organisation
    When I delete the connector "conn-1"
    Then the response status is 204

  Scenario: Deleting a connector from another organisation is a 404
    Given a connector with id "conn-1" exists in another organisation
    When I delete the connector "conn-1"
    Then the response status is 404
