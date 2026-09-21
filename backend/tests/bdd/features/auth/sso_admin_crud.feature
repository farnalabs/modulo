Feature: SSO provider admin CRUD
  As an organisation admin
  I want to configure, list, test and delete SSO providers through the admin API
  So that OIDC and SAML sign-in is under operator control

  The `/api/v1/admin/sso/providers` surface hosts the SSO provider lifecycle
  (PRD 9.4): `GET` lists the configured providers, `POST` creates an OIDC or
  SAML 2.0 provider (201), `PUT /providers/{id}` updates a provider (empty body
  -> 400), `PUT /providers/{id}/toggle` enables/disables it, `DELETE` removes it
  (204), `POST /providers/{id}/test` verifies the connection (OIDC resolves the
  discovery document, SAML parses the metadata XML), and the
  `PUT`/`GET /providers/{id}/group-mappings` surface manages group-to-team
  mappings. Duplicate provider names -> 409, invalid provider type / default role
  -> 422, a missing provider -> 404, and non-admin callers -> 403. These
  scenarios drive the real routes with only the DB CRUD, RLS and outbound
  network seams patched.

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin lists the configured SSO providers with their types
    Given the SSO provider store has one OIDC provider and one SAML provider
    When I list the configured SSO providers
    Then the response status is 200
    And the provider list contains the OIDC provider and the SAML provider

  Scenario: Admin creates an OIDC provider
    Given the SSO provider store accepts a new provider
    When I create the OIDC provider "Google Workspace" with discovery URL "https://accounts.example.com/.well-known/openid-configuration"
    Then the response status is 201
    And the created provider is an OIDC provider with a callback URL
    And the created provider does not echo the client secret

  Scenario: Admin creates a SAML 2.0 provider
    Given the SSO provider store accepts a new provider
    When I create the SAML provider "Okta" with metadata URL "https://idp.example.com/metadata"
    Then the response status is 201
    And the created provider is a SAML provider

  Scenario: Creating an unrestricted provider is rejected while the flag is off
    Given the SSO provider store accepts an unrestricted provider
    And the unrestricted provisioning flag is off
    When I create the OIDC provider "Open SSO" with discovery URL "https://accounts.example.com/.well-known/openid-configuration"
    Then the response status is 422
    And the error detail mentions "sso_unrestricted_provisioning"

  Scenario: Creating a provider with a duplicate name is a 409
    Given the SSO provider store rejects a duplicate provider name
    When I create the OIDC provider "Google Workspace" with discovery URL "https://accounts.example.com/.well-known/openid-configuration"
    Then the response status is 409
    And the error detail mentions "already exists"

  Scenario: Creating a provider with an invalid type is a 422
    When I create an SSO provider with an invalid provider type
    Then the response status is 422

  Scenario: Admin updates a provider
    Given an OIDC provider with id "prov-1" exists
    When I rename the provider "prov-1" to "Renamed Provider"
    Then the response status is 200
    And the response has name "Renamed Provider"

  Scenario: Updating a provider with an empty body is a 400
    Given an OIDC provider with id "prov-1" exists
    When I update the provider "prov-1" with an empty body
    Then the response status is 400

  Scenario: Updating a missing provider is a 404
    When I rename the provider "prov-1" to "Renamed Provider"
    Then the response status is 404

  Scenario: Admin disables a provider with the toggle
    Given an OIDC provider with id "prov-1" exists
    When I toggle the provider "prov-1"
    Then the response status is 200
    And the provider reports enabled false

  Scenario: Admin deletes a provider
    Given an OIDC provider with id "prov-1" exists
    When I delete the provider "prov-1"
    Then the response status is 204

  Scenario: Deleting a missing provider is a 404
    When I delete the provider "prov-1"
    Then the response status is 404

  Scenario: Testing an OIDC provider connection resolves the discovery document
    Given an OIDC provider with id "prov-1" exists
    And the OIDC discovery endpoint responds with a valid discovery document
    When I test the connection for the provider "prov-1"
    Then the response status is 200
    And the connection test reports success
    And the discovered OIDC endpoints include an authorization endpoint

  Scenario: Testing a SAML provider connection parses the metadata XML
    Given a SAML provider with id "prov-2" and local metadata XML exists
    When I test the connection for the provider "prov-2"
    Then the response status is 200
    And the connection test reports success
    And the parsed SAML metadata exposes an entity id

  Scenario: Group-to-team mappings can be set and retrieved
    Given an OIDC provider with id "prov-1" exists
    When I set the group-to-team mappings for the provider "prov-1"
    Then the response status is 200
    And the mapping response contains the group-to-team mappings
    When I retrieve the group-to-team mappings for the provider "prov-1"
    Then the response status is 200
    And the mapping response contains the group-to-team mappings

  Scenario: Non-admin operators cannot manage SSO providers
    Given I am authenticated as a non-admin user
    When I list the configured SSO providers
    Then the response status is 403
