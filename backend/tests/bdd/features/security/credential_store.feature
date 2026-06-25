Feature: Credential Store
  As a security-conscious operator
  I want all connector credentials to be encrypted at rest using Fernet
  So that a database breach does not expose secrets

  Scenario: Credential is encrypted on write
    Given a connector with API key "my-secret-api-key"
    When I save the connector
    Then the stored credential value is a Fernet token
    And decrypting with FERNET_KEY yields "my-secret-api-key"

  Scenario: Credential is decrypted on read within a pipeline node
    Given a connector with encrypted credential
    When a pipeline node calls connector.query()
    Then the node receives the plaintext credential

  Scenario: Wrong FERNET_KEY cannot decrypt
    Given a credential encrypted with key A
    When the service restarts with key B
    Then attempting to decrypt raises InvalidToken
