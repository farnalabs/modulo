Feature: Linear Connector
  As a pipeline author
  I want to interact with Linear via the thin connector
  So that I can resolve, read, and apply scoped writes to issues

  Background:
    Given a Linear connector with a valid API key

  Scenario: Resolve an issue by identifier (T1)
    When I resolve the Linear issue "ENG-123"
    Then the resolved fact has title "Flaky deploy"
    And the resolved fact has status "In Progress"
    And the resolved fact has assignee "Dana"
    And the resolved fact has a link

  Scenario: Resolve an issue by internal id (T1)
    When I resolve the Linear issue "c1a2b3c4-0000-0000-0000-000000000001"
    Then the resolved fact has identifier "ENG-123"

  Scenario: Read the issue body (T2)
    When I read the Linear issue body for "ENG-123"
    Then the issue body contains "flakes"

  Scenario: Read the issue comments (T2)
    When I read the Linear issue comments for "ENG-123"
    Then the comment list has 2 comments

  Scenario: Update the issue status (T3 scoped write)
    When I update the Linear issue "ENG-123" status to "Done"
    Then the updated status is "Done"

  Scenario: Post a prefixed comment (T3 scoped write)
    When I comment on the Linear issue "ENG-123" with "hello"
    Then the comment body is prefixed with "[Modulo] "
    And the comment is created

  Scenario: Posting an already-prefixed comment is not double-prefixed
    When I comment on the Linear issue "ENG-123" with "[Modulo] hello"
    Then the comment body is "[Modulo] hello"

  Scenario: Resolving a missing issue fails
    When I resolve the Linear issue "NOPE-1"
    Then the result is an error

  Scenario: Commenting without an issue reference fails
    When I comment on the Linear issue "" with "hello"
    Then the result is an error
