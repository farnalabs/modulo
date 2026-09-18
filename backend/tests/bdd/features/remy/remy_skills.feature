Feature: Remy Skill Management
  As an admin or user
  I want to manage skills for the Remy AI assistant
  So that I can customise its behaviour with reusable instructions

  Scenario: Admin creates an org-level skill
    Given I am authenticated as an admin in org "acme"
    When I create an org skill with name "code-review" and body "Review code for bugs"
    Then the response status is 201
    And the skill response has name "code-review"
    And the skill response is active

  Scenario: Admin lists org-level skills
    Given I am authenticated as an admin in org "acme"
    And an org skill "code-review" exists
    And an org skill "security-audit" exists
    When I list org skills
    Then the response status is 200
    And the response contains 2 skills

  Scenario: Admin updates an org-level skill
    Given I am authenticated as an admin in org "acme"
    And an org skill "code-review" exists
    When I update the org skill name to "code-review-v2"
    Then the response status is 200
    And the skill response has name "code-review-v2"

  Scenario: Admin deletes an org-level skill
    Given I am authenticated as an admin in org "acme"
    And an org skill "code-review" exists
    When I delete the org skill
    Then the response status is 204

  Scenario: User creates a personal skill
    Given I am authenticated as an admin in org "acme"
    When I create a user skill with name "my-prompt" and body "Always be positive"
    Then the response status is 201
    And the skill response has name "my-prompt"

  Scenario: User lists own skills
    Given I am authenticated as an admin in org "acme"
    And a user skill "favourite-quote" exists
    When I list user skills
    Then the response status is 200
    And the response contains 1 skill

  Scenario: User cannot see org-level skills in their own endpoint
    Given I am authenticated as an admin in org "acme"
    And an org skill "code-review" exists
    And a user skill "my-prompt" exists
    When I list user skills
    Then the response contains 1 skill
    And the skill is not named "code-review"

  Scenario: Non-admin cannot create org-level skill
    Given I am authenticated as a viewer in org "acme"
    When I create an org skill with name "rogue" and body "hack"
    Then the response status is 403

  Scenario: Update non-existent org skill returns 404
    Given I am authenticated as an admin in org "acme"
    When I update an org skill by id "00000000-0000-0000-0000-000000099999" with name "ghost"
    Then the response status is 404

  Scenario: Delete non-existent org skill returns 404
    Given I am authenticated as an admin in org "acme"
    When I delete an org skill by id "00000000-0000-0000-0000-000000099999"
    Then the response status is 404
