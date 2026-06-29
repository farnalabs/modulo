Feature: SSO OIDC Integration
  As a user
  I want to log in with Google or GitHub via OIDC
  So that I can access Modulo without managing a separate password

  Background:
    Given OIDC providers "google" and "github" are configured

  Scenario: OIDC login initiates redirect to provider
    When I initiate OIDC login with "google"
    Then I am redirected to the OIDC provider
    And the redirect URL contains the OIDC authorization endpoint

  Scenario: Callback creates new user via JIT provisioning
    Given a first-time OIDC user with email "newuser@example.com"
    When the OIDC callback returns a valid authorization code and state
    Then the response status is 307
    And the redirect URL contains access and refresh tokens
    And a new user account was provisioned

  Scenario: Returning OIDC user is logged in without duplicate
    Given an existing OIDC user with email "alice@example.com"
    When the OIDC callback returns a valid authorization code and state
    Then the response status is 307
    And the redirect URL contains access and refresh tokens
    And no duplicate account was created

  Scenario: State parameter guards against CSRF
    When the OIDC callback returns a valid code with a tampered state
    Then the response status is 401
    And the error detail mentions "CSRF"

  Scenario: Missing callback parameters are rejected
    When the OIDC callback returns without code or state
    Then the response status is 400

  Scenario: Enterprise gate blocks OIDC on free tier
    Given I do not have an enterprise license
    When I initiate OIDC login with "google"
    Then the response status is 402
    And the error detail mentions "sso"

  Scenario: Unknown provider is rejected
    When I initiate OIDC login with "unknown"
    Then the response status is 400
