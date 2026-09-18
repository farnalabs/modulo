Feature: Run Retention
  As a platform operator
  I want terminal runs purged after a configurable TTL
  So that storage costs are controlled and stale data is removed

  Scenario: Run auto-deleted after retention TTL
    Given a terminal run exists that completed 95 days ago
    And the retention TTL is 90 days
    When the nightly retention job runs
    Then the run is deleted
    And the run's LangGraph checkpoints are deleted
    And the nightly retention job acquired advisory lock "run_retention_job"
    And the retention job processed runs in batches of 500

  Scenario: Active runs not deleted by retention job
    Given a run exists with status "running"
    And a run exists with status "pending"
    And a terminal run exists that completed 95 days ago
    When the nightly retention job runs
    Then only the terminal run is deleted
    And the running run is preserved
    And the pending run is preserved

  Scenario: Admin manual purge with date filter
    Given I am authenticated as an admin in org "acme"
    And terminal runs exist with completed_at before "2026-01-01"
    And terminal runs exist with completed_at after "2026-01-01"
    When I POST /api/admin/purge with {"older_than": "2026-01-01"}
    Then the response status is 200
    And the purge response contains deleted_run_count
    And only runs completed before 2026-01-01 are deleted

  Scenario: Purge audit logged
    Given I am authenticated as an admin in org "acme"
    When I POST /api/admin/purge with {"older_than": "2026-01-01"}
    Then an audit event "run_purge" is recorded
    And the audit event includes the admin user id
    And the audit event includes the date filter used

  Scenario: Configurable retention period
    Given org "acme" has retention TTL of 45 days
    And a terminal run exists that completed 60 days ago
    And a terminal run exists that completed 30 days ago
    When the nightly retention job runs
    Then the run completed 60 days ago is deleted
    And the run completed 30 days ago is preserved

  Scenario: Purge respects org isolation
    Given I am authenticated as an admin in org "acme"
    And terminal runs exist in org "acme" completed before "2026-01-01"
    And terminal runs exist in org "globex" completed before "2026-01-01"
    When I POST /api/admin/purge with {"older_than": "2026-01-01"}
    Then only runs belonging to org "acme" are deleted
    And runs belonging to org "globex" are preserved
