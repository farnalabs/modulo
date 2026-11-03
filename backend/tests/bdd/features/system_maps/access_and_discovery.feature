Feature: System Maps internal access and discovery
  As an internal operator
  I want the System Maps surface to stay behind its internal gate
  So that an ordinary caller cannot inspect or draft organisation maps

  System Maps is an internal preview surface. The REST router is gated by the
  `system_maps` feature flag, developer mode, an operator/admin role and the
  dedicated `system_maps_access` grant. Discovery inspects the organisation
  inventory and persists an UNPUBLISHED draft with suggested edges.

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: An authorised internal operator discovers an unpublished draft
    Given the organisation inventory has a pipeline, a trigger, a connector and a folder
    When I request System Maps discovery for a map named "Dogfood Map"
    Then the response status is 201
    And the discovery report is unpublished and names the draft
    And the discovery report infers suggested edges
    And the persisted discovery draft records honest stage types
    And the discovery reads are tenant scoped

  Scenario: An ordinary viewer is refused
    Given I am authenticated as a viewer in org "acme"
    When I request System Maps discovery for a map named "Viewer Map"
    Then the response status is 403
    And the discovery denial names the required role

  Scenario: An operator without the system maps grant is refused
    Given the caller's credential carries no system maps grant
    When I request System Maps discovery for a map named "Grantless Map"
    Then the response status is 403
    And the discovery denial reports the key is not granted

  Scenario: Developer mode off refuses even an entitled operator
    Given developer mode is disabled for the instance
    When I request System Maps discovery for a map named "No Dev Mode"
    Then the response status is 403
    And the discovery denial requires developer mode

  Scenario: The system maps feature flag refuses the surface
    Given the system maps feature is disabled on the plan
    When I request System Maps discovery for a map named "No Flag"
    Then the response status is 402
    And the discovery denial names the system_maps feature
