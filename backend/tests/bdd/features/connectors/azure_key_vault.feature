Feature: Azure Key Vault Connector
  As a pipeline author
  I want to read and manage secrets, keys, and certificates in Azure Key Vault
  So that my agents can securely access and manage credentials

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates token
    Given an Azure Key Vault connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given an Azure Key Vault connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists secrets
    Given an Azure Key Vault connector configured with valid credentials
    When the connector queries secrets
    Then the result contains secret metadata

  Scenario: Connector gets a secret value
    Given an Azure Key Vault connector configured with valid credentials
    When the connector queries secret "my-secret"
    Then the result contains the secret value

  Scenario: Connector lists keys
    Given an Azure Key Vault connector configured with valid credentials
    When the connector queries keys
    Then the result contains key metadata

  Scenario: Connector gets a key
    Given an Azure Key Vault connector configured with valid credentials
    When the connector queries key "my-key"
    Then the result contains the key details

  Scenario: Connector lists certificates
    Given an Azure Key Vault connector configured with valid credentials
    When the connector queries certificates
    Then the result contains certificate metadata

  Scenario: Connector gets a certificate
    Given an Azure Key Vault connector configured with valid credentials
    When the connector queries certificate "my-cert"
    Then the result contains the certificate details

  Scenario: Connector creates a secret
    Given an Azure Key Vault connector configured with valid credentials
    When the connector creates secret "new-secret" with value "s3cret"
    Then the secret is created successfully

  Scenario: Connector deletes a secret
    Given an Azure Key Vault connector configured with valid credentials
    When the connector deletes secret "old-secret"
    Then the secret is soft-deleted
