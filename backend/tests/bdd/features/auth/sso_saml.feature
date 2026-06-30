Feature: SSO SAML 2.0 Integration
  As a user
  I want to log in via SAML 2.0 identity providers
  So that I can access Modulo using Team SSO with JIT provisioning

  Background:
    Given SAML 2.0 is enabled with provider "https://idp.example.com"

  Scenario: SP metadata endpoint returns valid XML
    When I request the SAML SP metadata endpoint
    Then the response status is 200
    And the response contains valid SAML metadata XML
    And the metadata includes the ACS endpoint URL

  Scenario: ACS callback creates new user via JIT provisioning
    Given a first-time SAML user with email "newuser@example.com"
    When the SAML ACS endpoint receives a valid SAMLResponse
    Then the response status is 307
    And the redirect URL contains access and refresh tokens
    And a new user account was provisioned

  Scenario: Existing SAML user is logged in without duplicate
    Given an existing SAML user with email "alice@example.com"
    When the SAML ACS endpoint receives a valid SAMLResponse
    Then the response status is 307
    And the redirect URL contains access and refresh tokens
    And no duplicate account was created

  Scenario: Invalid SAML response is rejected
    When the SAML ACS endpoint receives a malformed SAMLResponse
    Then the response status is 401
    And the error detail mentions "SAML Assertion"

  Scenario: Missing SAMLResponse in form data is rejected
    When the SAML ACS endpoint receives a request without SAMLResponse
    Then the response status is 400
    And the error detail mentions "SAMLResponse"

  Scenario: IdP group claim maps to team membership
    Given SAML group mapping is configured for "admins" to team "team-1" with role "admin"
    When the SAML ACS endpoint receives a SAMLResponse with groups "admins,developers"
    Then the response status is 307
    And the user is added to team "team-1" with role "admin"

  Scenario: Team gate blocks SAML on Community tier
    Given I do not have a Team license
    When I request the SAML SP metadata endpoint
    Then the response status is 402
    And the error detail mentions "sso"

  Scenario: SAML login initiates redirect to IdP
    When I initiate SAML login
    Then the response status is 307
    And I am redirected to the SAML IdP single sign-on URL
