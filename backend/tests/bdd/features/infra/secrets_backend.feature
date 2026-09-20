Feature: At-rest Secret Storage (Secrets Backend)
  As a platform operator
  I want connector credentials, model-backend API keys and notification
    endpoints to be encrypted at rest and scoped to the organisation that
    owns them
  So that secret material is never stored in plaintext and never leaks
    across tenants (feat-core-secrets-backend)

  Scenario: Storing and reading a secret round-trips the plaintext
    Given a Fernet secret store
    When I store the secret "db-api-key" as "sup3r-s3cret"
    And I read the secret "db-api-key"
    Then the secret "db-api-key" is "sup3r-s3cret"

  Scenario: Overwriting a secret upserts the stored ciphertext in place
    Given a Fernet secret store
    When I store the secret "db-api-key" as "first-value"
    And I store the secret "db-api-key" as "second-value"
    And I read the secret "db-api-key"
    Then the secret "db-api-key" is "second-value"

  Scenario: Deleting a secret removes it from the store
    Given a Fernet secret store
    When I store the secret "ephemeral-key" as "to-be-deleted"
    And I delete the secret "ephemeral-key"
    When I read the secret "ephemeral-key"
    Then the read fails with KeyError

  Scenario: A blank secret key is rejected before it touches the store
    Given a Fernet secret store
    When I store a secret with a blank key
    Then a ValueError is raised mentioning "non-empty"

  Scenario: Secret keys are normalised by stripping surrounding whitespace
    Given a Fernet secret store
    When I store the secret "  padded-key  " as "normalised"
    And I read the secret "padded-key"
    Then the secret "padded-key" is "normalised"

  Scenario: Secrets are scoped to their organisation
    Given a Fernet secret store shared by organisations A and B
    When organisation A stores the secret "shared-key" as "org-a-only"
    Then organisation A can read "shared-key" as "org-a-only"
    When organisation B reads the secret "shared-key"
    Then the read fails with KeyError

  Scenario: Reading or writing without a DB session fails closed
    Given a Fernet secret store with no DB session
    When I store the secret "some-key" as "some-value"
    Then a RuntimeError is raised mentioning "no DB session"

  Scenario: Writing without an RLS organisation context fails closed
    Given a Fernet secret store with no RLS organisation context
    When I store the secret "some-key" as "some-value"
    Then a RuntimeError is raised mentioning "RLS organisation context"

  Scenario: A secret encrypted under a rotated-out key remains readable
    Given the store holds a secret encrypted under a rotated-out key
    When I read the secret "rotated-key"
    Then the secret "rotated-key" is "old-value"

  Scenario: A secret that no held key can decrypt fails closed
    Given the store holds a secret encrypted under an unknown key
    When I read the secret "alien-key"
    Then the read fails with a ValueError mentioning "Failed to decrypt secret"

  Scenario: Corrupted at-rest ciphertext fails closed
    Given the store holds corrupted ciphertext for a secret
    When I read the secret "corrupt-key"
    Then the read fails with a ValueError mentioning "Failed to decrypt secret"

  Scenario: The factory defaults to the Fernet store
    When I ask the factory for the default secret backend
    Then the factory returns a Fernet backend

  Scenario: An unknown backend name is rejected with the choices
    When I ask the factory for the "consul" secret backend
    Then a ValueError is raised mentioning "fernet"

  Scenario: An unlicensed external backend falls back to the Fernet store
    When I ask the factory for the "vault" secret backend without a license
    Then the factory returns a Fernet backend
