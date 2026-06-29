Feature: Community Registry
  As a pipeline author
  I want to browse, publish, pull and verify signed registry primitives
  So that I can discover and trust community-contributed building blocks

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Browse registry primitives
    When the user requests GET /api/v1/registry/primitives
    Then the response status is 200
    And the response body has key "items" with an array
    And the response body has key "total"
    And each item has entry, publisher_status, publisher_name, and popularity_score

  Scenario: Search registry by keyword
    When the user requests GET /api/v1/registry/primitives?search=prd
    Then the response status is 200
    And each returned entry matches the search term "prd"

  Scenario: Get primitive detail by slug
    When the user requests GET /api/v1/registry/primitives/modulo/prd-input-schema
    Then the response status is 200
    And the response body has key "entry"
    And the entry has slug "modulo/prd-input-schema"

  Scenario: Primitive not found returns 404
    When the user requests GET /api/v1/registry/primitives/unknown/nope
    Then the response status is 404

  Scenario: Publish primitive (v1 protocol)
    When the user publishes a v1 primitive as "communityuser/my-workflow"
    Then the response status is 201
    And the response body has key "slug"
    And the slug matches the author and name

  Scenario: Publish primitive with Ed25519 signature (v2 protocol)
    When the user publishes a signed v2 primitive as "signeduser/signed-flow"
    Then the response status is 201
    And the response body has key "verified"
    And the response body has key "trust_anchor_verified"

  Scenario: Pull primitive to local library
    When the user downloads registry primitive "modulo/prd-input-schema"
    Then the response status is 200
    And the response body has key "entry"
    And the response body has key "verified"
    And the response body has key "integrity_ok"

  Scenario: Verify primitive signature
    When the user requests GET /api/v1/registry/verify/modulo/prd-input-schema
    Then the response status is 200
    And the response body has key "verified"
    And the response body has key "signing_key_fingerprint"

  Scenario: Register verified publisher
    When the user registers a publisher "verifieduser" with key "abcdef1234567890"
    Then the response status is 201
    And the response body has status "registered"

  Scenario: Revoke publisher
    Given the registry has a publisher "verifieduser" with key "abcdef1234567890"
    When the user sends POST /api/v1/registry/publishers/abcdef1234567890/revoke
    Then the response status is 200
    And the response body has status "revoked"

  Scenario: Trust anchor verification with PEM public key
    When the user requests GET /api/v1/registry/verify/modulo/prd-input-schema?public_key_pem=LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KZmFrZQotLS0tLUVORCBQVUJMSUMgS0VZLS0tLS0K
    Then the response status is 200
    And the response body has key "trust_anchor_verified"

  Scenario: Cross-org isolation for downloaded primitives
    Given a user in org "beta" downloads "modulo/prd-input-schema"
    When the "acme" org requests GET /api/v1/libraries
    Then the response contains no primitives from org "beta"
