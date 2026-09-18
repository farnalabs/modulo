Feature: JWT Token Lifecycle and Purpose Isolation
  As an application security boundary
  I want every token to be verifiably signed, expiring, purpose-scoped and tied
    to tenant identity
  So that a token minted for one purpose (API access, WebSocket, refresh, HITL
    claim) can never be replayed in another (feat-auth-jwt-auth)

  Background:
    Given the signing secret is "a_sufficiently_long_secret_key_32b"
    And the tenant org is "00000000-0000-0000-0000-000000000001"
    And the user account is "11111111-1111-1111-1111-111111111111"

  Scenario: An access token round-trips identity, tenant, role and credential class
    When an access token is minted for user "alice" with role "admin" and client kind "browser"
    And I decode the token as a principal
    Then decoding succeeds
    And the principal has username "alice"
    And the principal has role "admin"
    And the principal belongs to the minted org
    And the principal has client kind "browser"

  Scenario: A token signed with a different secret is rejected
    When an access token is minted for user "alice" with role "admin" and client kind "browser"
    And the token is decoded with a different secret
    Then decoding is rejected

  Scenario: A tampered signature is rejected
    When an access token is minted for user "alice" with role "admin" and client kind "browser"
    And the token signature is tampered with
    And I decode the token as a principal
    Then decoding is rejected

  Scenario: An expired access token is rejected
    When an expired access token is minted for user "alice" with role "admin"
    And I decode the token as a principal
    Then decoding is rejected

  Scenario: A forged alg=none token is rejected
    When a token is forged with algorithm "none" for user "alice" with role "admin"
    And I decode the token as a principal
    Then decoding is rejected

  Scenario: A token missing subject identity is rejected
    When a token is minted with no subject and no account identity
    And I decode the token as a principal
    Then decoding is rejected

  Scenario Outline: A token is only usable within its own purpose
    Given a "<kind>" token is minted for user "alice"
    When I decode the token as a principal for the "<purpose>" purpose
    Then decoding is <outcome>

    Examples:
      | kind      | purpose   | outcome   |
      | access    | ws        | rejected  |
      | access    | refresh   | rejected  |
      | websocket | ws        | accepted  |
      | websocket | refresh   | rejected  |
      | refresh   | ws        | rejected  |
      | refresh   | refresh   | accepted  |

  Scenario: Rotating a refresh token mints an access token that keeps the original credential class
    When a refresh token is minted for user "alice" with role "operator" and client kind "programmatic" in family "family-a" at sequence 1
    And I rotate the refresh token
    And I decode the rotated token as a principal
    Then decoding succeeds
    And the principal has username "alice"
    And the principal has role "operator"
    And the principal has client kind "programmatic"

  Scenario: Rotation refuses a websocket token
    When a websocket token is minted for user "alice" with role "admin"
    And I rotate the token
    Then rotation is rejected

  Scenario: A claim token scopes a HITL gate decision to one run and gate
    When a claim token is minted for user "alice" for run "22222222-2222-2222-2222-222222222222" and gate "review-step"
    And I decode the claim token against run "22222222-2222-2222-2222-222222222222" and gate "review-step"
    Then the claim token is accepted
    And the claim token carries run "22222222-2222-2222-2222-222222222222" and gate "review-step"
    And decoding the claim token against the wrong gate "other-step" is rejected

  Scenario: A legacy token without a credential class decodes as a browser
    When a legacy token is minted for user "alice" with role "admin"
    And I decode the token as a principal
    Then decoding succeeds
    And the principal has client kind "browser"
