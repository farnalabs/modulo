Feature: Multi-Backend Database Support
  As a Modulo operator
  I want tenant isolation, migrations, locks, and time functions to work consistently across Postgres, MariaDB, and SQLite
  So that the application behaves correctly regardless of the chosen database backend

  Scenario: SQLite tenant filtering via GenericRepository
    Given a GenericRepository connected to SQLite
    When set_org_context is called with org "acme"
    Then the session stores org_id in session.info
    And apply_tenant_filter injects WHERE organisation_id = :org_id

  Scenario: MariaDB tenant filtering via GenericRepository
    Given a GenericRepository connected to MariaDB
    When set_org_context is called with org "acme"
    Then the session stores org_id in session.info
    And apply_tenant_filter injects WHERE organisation_id = :org_id

  Scenario: Postgres uses RLS natively
    Given a PostgresRepository connected to Postgres
    When set_org_context is called with org "acme"
    Then set_config('app.organisation_id', :oid, true) is executed
    And apply_tenant_filter returns the statement unchanged

  Scenario: Cross-org isolation on SQLite
    Given entity records belonging to org "acme" and org "othercorp"
    When the session is scoped to org "othercorp"
    And a SELECT query is executed through GenericRepository
    Then only records for org "othercorp" are returned

  Scenario: Alembic migration runs on all backends
    Given a SQLite, MariaDB, and Postgres database URL
    When the migration env.py configures the backend
    Then render_as_batch is enabled for SQLite
    And the async-to-sync driver conversion succeeds for each backend

  Scenario: Advisory lock abstraction
    Given a lock service for Postgres
    When a lock is acquired for key "pipeline:42"
    Then pg_try_advisory_lock is called
    Given a lock service for SQLite
    When a lock is acquired for the same key
    Then an asyncio.Lock is used instead

  Scenario: Time functions work on all backends
    Given a model with created_at using func.now()
    When the model is persisted to SQLite
    Then the timestamp is set to the current time
    And the same behaviour holds for MariaDB and Postgres
