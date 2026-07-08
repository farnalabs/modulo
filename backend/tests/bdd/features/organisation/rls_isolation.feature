Feature: RLS Isolation
  As a database administrator
  I want Row-Level Security enforced at the database level
  So that querying across organisations is impossible even if the app layer is bypassed

  Scenario: RLS restricts raw queries to current org
    Given org "acme" has pipeline "my-pipeline"
    And org "othercorp" has pipeline "secret-pipeline"
    When a raw query runs with SET app.organisation_id = 'acme'
    Then the query returns only "my-pipeline"

  Scenario: RLS without SET LOCAL is rejected
    When a raw query runs without setting app.current_org_id
    Then the query returns no rows

  Scenario: Cross-org query returns empty
    Given org "acme" has pipeline "my-pipeline"
    When a raw query runs with SET app.organisation_id = 'othercorp'
    Then the query returns no rows

  Scenario: Advisory lock respects organisation scope
    Given I am authenticated in org "acme"
    When I acquire a pipeline lock for "my-pipeline"
    Then org "othercorp" can also acquire a lock for their pipeline

  Scenario: RLS policy exists on all resource tables
    Given the database has been migrated
    When I inspect table policies
    Then every resource table has an RLS policy on organisation_id
