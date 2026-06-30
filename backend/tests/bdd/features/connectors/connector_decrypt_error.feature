Feature: ConnectorDecryptError Handling
  As a pipeline author
  I want credential decryption failures to produce clear errors
  So that I can diagnose misconfigured connector instances

  Scenario: Missing secret raises ConnectorDecryptError
    Given a connector instance with no secret in the backend
    When I initialise the connector hub with that instance
    Then a ConnectorDecryptError is raised with the connector ID

  Scenario: Invalid JSON in stored secret raises ConnectorDecryptError
    Given a connector instance with malformed JSON in the stored secret
    When I initialise the connector hub with that instance
    Then a ConnectorDecryptError is raised with the connector ID
