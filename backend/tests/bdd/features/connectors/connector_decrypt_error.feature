Feature: ConnectorDecryptError Handling
  As a pipeline author
  I want credential decryption failures to produce clear errors
  So that I can diagnose misconfigured connector instances

  Scenario: Missing secret initialises with empty credentials
    Given a connector instance with no secret in the backend
    When I initialise the connector hub with that instance
    Then the connector initialises with empty credentials

  Scenario: Invalid JSON in stored secret skips the connector
    Given a connector instance with malformed JSON in the stored secret
    When I initialise the connector hub with that instance
    Then the connector is skipped on decrypt failure
