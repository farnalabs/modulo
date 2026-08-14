Feature: Node Category Deletion Referential Integrity
  As an organisation operator
  I want deleting a node category to be refused while pipeline nodes still reference it
  So that pipeline graphs never dangle a reference to a deleted category

  Scenario: An unreferenced node category is deleted
    Given I am an operator of org "acme"
    And a node category exists that is not referenced by any pipeline node
    When I delete the node category
    Then the response status is 204

  Scenario: A node category referenced by a pipeline node is refused
    Given I am an operator of org "acme"
    And a node category exists
    And pipeline "Onboarding flow" has a node using the node category
    When I delete the node category
    Then the response status is 409
    And the response detail lists the referencing pipeline "Onboarding flow"
    And the node category still exists

  Scenario: A viewer cannot delete a node category
    Given I am a viewer of org "acme"
    And a node category exists
    When I delete the node category
    Then the response status is 403
