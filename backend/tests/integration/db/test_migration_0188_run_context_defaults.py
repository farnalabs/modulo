"""Prove-the-fix test for migration 0188_pipeline_run_context_defaults_default.

Before 0188, ``pipelines.run_context_defaults`` was ``NOT NULL`` with no
``server_default``, so any raw ``INSERT INTO pipelines`` that omitted the column
(e.g. integration tests exercising the CHECK constraints) failed with a NOT NULL
violation before the intended behaviour could be exercised. Migration 0188 adds
``DEFAULT '{}'::json`` so direct inserts fall back to an empty JSON object.

This test exercises the exact failing path on the migrated schema: it drops the
default (within the session transaction) to reproduce the pre-0188 state, asserts
a raw insert that omits the column raises ``IntegrityError``, then rolls that back
(restoring the default) and asserts the same raw insert now succeeds with ``{}``.
Nothing is committed, so the shared integration schema is left untouched.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.integration]


async def test_run_context_defaults_server_default_allows_raw_insert_without_column(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    insert_sql = text("INSERT INTO pipelines (id, organisation_id, name, account_id) VALUES (:id, :oid, :name, :aid)")

    # Pre-0188 state: drop the server default inside a savepoint so the aborted
    # raw insert can be rolled back to the savepoint WITHOUT killing the outer
    # RLS-scoped transaction. set_config(..., is_local=true) (set by the
    # rls_session fixture) is bound to the top-level transaction, so that
    # transaction must stay alive for the positive case to remain tenant-scoped.
    savepoint = await rls_session.begin_nested()
    await rls_session.execute(text("ALTER TABLE pipelines ALTER COLUMN run_context_defaults DROP DEFAULT"))

    # A raw insert omitting run_context_defaults must fail with NOT NULL.
    with pytest.raises(IntegrityError):
        await rls_session.execute(
            insert_sql,
            {
                "id": str(uuid.uuid4()),
                "oid": str(test_org),
                "name": "no-default-insert",
                "aid": str(test_user),
            },
        )
    # Roll back to the savepoint only: this restores the 0188 default while the
    # outer RLS transaction (and its tenant scope) stays active. The fixture
    # rolls the whole session back at teardown, so nothing is committed.
    await savepoint.rollback()

    # Post-0188: the default fills run_context_defaults with an empty object.
    ok_pid = uuid.uuid4()
    await rls_session.execute(
        insert_sql,
        {
            "id": str(ok_pid),
            "oid": str(test_org),
            "name": "default-insert",
            "aid": str(test_user),
        },
    )
    await rls_session.flush()
    stored = await rls_session.execute(
        text("SELECT run_context_defaults FROM pipelines WHERE id = :id"),
        {"id": str(ok_pid)},
    )
    assert not stored.scalar_one()
    # Discard the positive-insert row; the fixture's final rollback is a no-op.
    await rls_session.rollback()
