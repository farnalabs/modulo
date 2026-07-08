Feature: SSO Group-to-Team Mapping
  As an admin
  I want to map IdP groups to Modulo teams and roles
  So that JIT-provisioned users automatically get correct team membership

  Background:
    Given I am authenticated as an admin in org "org-1"

  Scenario: Admin configures group mapping for an SSO provider
    Given an SSO provider exists with id "prov-1"
    When I set group mappings for provider "prov-1"
      | idp_group   | team_id | team_role |
      | engineering | team-1  | operator  |
    Then the response status is 200
    And the group mappings are persisted

  Scenario: Admin retrieves configured group mappings
    Given an SSO provider with id "prov-1" has group mappings configured
    When I GET group mappings for provider "prov-1"
    Then the response status is 200
    And the response contains 1 mapping entry

  Scenario: Group mapping applied at OIDC JIT provisioning
    Given OIDC providers are configured
    And group mapping is configured for "engineering" to team "team-1" with role "operator"
    And a first-time OIDC user with email "alice@example.com"
    When the OIDC callback returns a valid code with IdP groups "engineering"
    Then the response status is 307
    And apply_group_mappings was called

  Scenario: Group mapping applied at SAML JIT provisioning
    Given SAML 2.0 is enabled with provider "https://idp.example.com"
    And group mapping is configured for "admins" to team "team-2" with role "admin"
    And a first-time SAML user with email "bob@example.com"
    When the SAML ACS endpoint receives a SAMLResponse with groups "admins"
    Then the response status is 307
    And apply_group_mappings was called

  Scenario: Multiple IdP groups map to multiple teams
    Given OIDC providers are configured
    And group mapping is configured for "engineering" to team "team-1" with role "operator"
    And group mapping is configured for "design" to team "team-2" with role "viewer"
    And a first-time OIDC user with email "charlie@example.com"
    When the OIDC callback returns a valid code with IdP groups "engineering,design"
    Then the response status is 307
    And apply_group_mappings was called with groups "engineering,design"

  Scenario: User receives the role specified in the group mapping
    Given OIDC providers are configured
    And group mapping is configured for "viewers" to team "team-3" with role "viewer"
    And a first-time OIDC user with email "dave@example.com"
    When the OIDC callback returns a valid code with IdP groups "viewers"
    Then the mapping assigns role "viewer" for the matched group

  Scenario: Unmatched IdP groups are silently ignored
    Given OIDC providers are configured
    And group mapping is configured for "engineering" to team "team-1" with role "operator"
    And a first-time OIDC user with email "eve@example.com"
    When the OIDC callback returns a valid code with IdP groups "unknown-group"
    Then the response status is 307
    And apply_group_mappings had no matching groups
