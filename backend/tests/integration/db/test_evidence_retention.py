"""Integration tests for evidence retention (FAR-961, chunk 9a §2.5/§2.7).

Real-Postgres (testcontainers) acceptance tests for the batched purge sweep:
the age/row-count policies against real SQL, the append-only independence
from run deletion (criterion 3), the schema-reflection contract that the
evidence table carries no FK to runs (criterion 6), the REAL per-org
advisory-lock mutual exclusion (criterion 9), and the concurrent-insert
race with a back-dated ``created_at`` (criterion 10 — deterministic here
via an uncommitted second connection, which SQLite cannot express).

The integration database persists across tests (session-scoped engine, no
truncation), so every test mints its OWN organisation and scopes every
evidence read to it — the same multi-tenant hygiene production relies on.

Mechanics-level coverage lives in ``tests/unit/core/evidence/test_retention.py``.

Spec criteria covered here: 1, 2, 3, 5 (log fields), 6, 9, 10.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.evidence_retention import (
    EvidenceRetentionPolicy,
    _org_lock_key,
    count_evidence_rows,
    purge_evidence,
)
from modulo.db.models.evidence import Evidence

pytestmark = pytest.mark.integration

_RETENTION_LOGGER = "modulo.core.evidence_retention"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _utc_days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@pytest.fixture
async def org(db_engine: AsyncEngine) -> uuid.UUID:
    """A FRESH organisation per test — the integration DB never truncates."""
    org_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": "retention-org", "slug": f"ret-{org_id.hex[:12]}"},
        )
    return org_id


def _add_evidence(
    session: AsyncSession,
    org_id: uuid.UUID,
    key: str,
    *,
    created_at: datetime,
    subject_type: str = "eval",
    subject_id: str = "subject-1",
    value: Any = None,
) -> Evidence:
    row = Evidence(
        organisation_id=org_id,
        key=key,
        subject_type=subject_type,
        subject_id=subject_id,
        value=value,
        observed_at=created_at,
        producer_type="eval",
        created_at=created_at,
    )
    session.add(row)
    return row


def _scoped(session_query: Any, org_id: uuid.UUID) -> Any:
    return session_query.where(Evidence.organisation_id == org_id)


async def _org_keys(session: AsyncSession, org_id: uuid.UUID) -> set[str]:
    result = await session.execute(_scoped(select(Evidence.key), org_id))
    return set(result.scalars().all())


async def _org_row_count(session: AsyncSession, org_id: uuid.UUID) -> int:
    return await count_evidence_rows(session, org_id)


async def _seed_pipeline_snapshot_run(engine: AsyncEngine, org_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Seed account + pipeline + snapshot + run rows (proven raw-SQL pattern)."""
    account_id, pipeline_id, snapshot_id, run_id = (uuid.uuid4() for _ in range(4))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {"id": str(account_id), "email": f"ret-{org_id.hex[:8]}@test.local", "name": "Retention"},
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": "retention-pipe", "aid": str(account_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, graph_json, "
                "connector_bindings_json, schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', 1, :ih, :tid)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "ih": "a" * 64,
                "tid": f"{org_id}:{run_id}",
            },
        )
    return {"account_id": account_id, "pipeline_id": pipeline_id, "snapshot_id": snapshot_id, "run_id": run_id}


class _ConcurrentInsertSavepoint:
    """``begin_nested`` wrapper that fires an INSERT inside the batch window.

    The writer session executes its INSERT after the sweep's batch SELECT has
    fixed the id list and before the DELETE runs — the exact concurrency
    window of spec criterion 10.  The writer's transaction stays open
    (uncommitted) so the row is invisible to the sweep's subsequent SELECTs,
    which makes the outcome deterministic on READ COMMITTED.
    """

    def __init__(self, inner: Any, writer: AsyncSession, statement: Any) -> None:
        self._inner = inner
        self._writer = writer
        self._statement = statement

    async def __aenter__(self) -> Any:
        await self._writer.execute(self._statement)
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc_info: object) -> Any:
        return await self._inner.__aexit__(*exc_info)


# ---------------------------------------------------------------------------
# Age policy (§2.5 criterion 1)
# ---------------------------------------------------------------------------


async def test_age_purge_deletes_old_rows_and_keeps_recent(db_session: AsyncSession, org: uuid.UUID) -> None:
    _add_evidence(db_session, org, "old.key", created_at=_utc_days_ago(100))
    _add_evidence(db_session, org, "recent.key", created_at=_utc_days_ago(30))
    await db_session.commit()

    result = await purge_evidence(db_session, org, EvidenceRetentionPolicy(max_age_days=90))

    assert result.rows_deleted == 1
    assert result.batches == 1
    assert result.max_age_days == 90
    assert await _org_keys(db_session, org) == {"recent.key"}


# ---------------------------------------------------------------------------
# Row-count policy (§2.5 criterion 2)
# ---------------------------------------------------------------------------


async def test_max_rows_purge_deletes_oldest_excess(db_session: AsyncSession, org: uuid.UUID) -> None:
    now = datetime.now(UTC)
    rows = [_add_evidence(db_session, org, f"row.{i}", created_at=now - timedelta(minutes=i)) for i in range(120)]
    await db_session.flush()
    # rows[i] has created_at = now - i minutes, so the 20 OLDEST are rows[100:].
    oldest_ids = {row.id for row in rows[100:]}
    await db_session.commit()

    result = await purge_evidence(db_session, org, EvidenceRetentionPolicy(max_rows=100))

    assert result.rows_deleted == 20
    assert result.max_rows == 100
    remaining = await db_session.execute(_scoped(select(Evidence.id), org))
    remaining_ids = set(remaining.scalars().all())
    assert remaining_ids.isdisjoint(oldest_ids)
    assert len(remaining_ids) == 100


async def test_max_rows_none_never_triggers_count_purge(db_session: AsyncSession, org: uuid.UUID) -> None:
    for i in range(5):
        _add_evidence(db_session, org, f"row.{i}", created_at=_utc_days_ago(1))
    await db_session.commit()

    result = await purge_evidence(db_session, org, EvidenceRetentionPolicy())

    assert result.rows_deleted == 0
    assert result.batches == 0
    assert await _org_row_count(db_session, org) == 5


async def test_purge_batches_oldest_first(db_session: AsyncSession, org: uuid.UUID) -> None:
    old = _utc_days_ago(120)
    for i in range(7):
        _add_evidence(db_session, org, f"row.{i}", created_at=old - timedelta(minutes=i))
    await db_session.commit()

    result = await purge_evidence(db_session, org, EvidenceRetentionPolicy(max_age_days=90, batch_size=3))

    assert result.rows_deleted == 7
    assert result.batches == 3
    assert await _org_row_count(db_session, org) == 0


async def test_purge_leaves_other_orgs_rows_untouched(
    db_session: AsyncSession, db_engine: AsyncEngine, org: uuid.UUID
) -> None:
    foreign_org = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(foreign_org), "name": "foreign", "slug": f"foreign-{foreign_org.hex[:12]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO evidence (id, organisation_id, key, subject_type, subject_id, producer_type, created_at) "
                "VALUES (:id, :oid, :k, 'eval', 's', 'eval', :created)"
            ),
            {"id": str(uuid.uuid4()), "oid": str(foreign_org), "k": "theirs.old", "created": _utc_days_ago(200)},
        )
    _add_evidence(db_session, org, "mine.old", created_at=_utc_days_ago(200))
    await db_session.commit()

    result = await purge_evidence(db_session, org, EvidenceRetentionPolicy(max_age_days=90))

    assert result.rows_deleted == 1
    assert not await _org_keys(db_session, org)
    assert await _org_keys(db_session, foreign_org) == {"theirs.old"}


# ---------------------------------------------------------------------------
# Runs-cascade independence (§2.5 criterion 3)
# ---------------------------------------------------------------------------


async def test_deleting_run_does_not_delete_run_subject_evidence(
    db_session: AsyncSession, db_engine: AsyncEngine, org: uuid.UUID
) -> None:
    seeded = await _seed_pipeline_snapshot_run(db_engine, org)
    _add_evidence(
        db_session,
        org,
        "run.subject.evidence",
        subject_type="run",
        subject_id=str(seeded["run_id"]),
        created_at=_utc_days_ago(1),
    )
    await db_session.commit()
    assert await _org_row_count(db_session, org) == 1

    async with db_engine.begin() as conn:
        delete_result = await conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": str(seeded["run_id"])})

    assert delete_result.rowcount == 1
    assert await _org_row_count(db_session, org) == 1


# ---------------------------------------------------------------------------
# Schema contract: no FK from evidence to runs (§2.5 criterion 6)
# ---------------------------------------------------------------------------


async def test_evidence_table_has_no_foreign_key_to_runs(db_engine: AsyncEngine) -> None:
    query = text(
        """
        SELECT DISTINCT ccu.table_name AS referenced_table
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.constraint_schema = kcu.constraint_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_name = 'evidence'
        """
    )
    async with db_engine.connect() as conn:
        referenced = {row.referenced_table for row in await conn.execute(query)}

    # 'organisations' being present proves the catalog query detects FKs at
    # all — the test fails both when a runs FK is added and when the query
    # itself is broken.
    assert "organisations" in referenced
    assert "runs" not in referenced


# ---------------------------------------------------------------------------
# Advisory lock, real Postgres (§2.5 criterion 9)
# ---------------------------------------------------------------------------


async def test_second_sweep_while_locked_exits_cleanly_then_restarts(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    org: uuid.UUID,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _add_evidence(db_session, org, "old.key", created_at=_utc_days_ago(100))
    await db_session.commit()

    k1, k2 = _org_lock_key(org.bytes)
    holder = await db_engine.connect()
    try:
        grabbed = (
            await holder.execute(text("SELECT pg_try_advisory_lock(:k1, :k2)"), {"k1": k1, "k2": k2})
        ).scalar_one()
        assert grabbed is True

        with caplog.at_level(logging.WARNING, logger=_RETENTION_LOGGER):
            result = await purge_evidence(db_session, org, EvidenceRetentionPolicy(lock_timeout_seconds=0.3))

        assert result.rows_deleted == 0
        assert result.batches == 0
        assert await _org_row_count(db_session, org) == 1
        timeout_records = [record for record in caplog.records if record.msg == "evidence.retention.lock_timeout"]
        assert len(timeout_records) == 1
    finally:
        await holder.close()

    retry = await purge_evidence(db_session, org, EvidenceRetentionPolicy(lock_timeout_seconds=5.0))

    assert retry.rows_deleted == 1
    assert await _org_row_count(db_session, org) == 0


# ---------------------------------------------------------------------------
# Concurrent insert during the batch window (§2.5 criterion 10)
# ---------------------------------------------------------------------------


async def test_concurrent_insert_with_old_created_at_survives_sweep(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    org: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_evidence(db_session, org, "old.key", created_at=_utc_days_ago(100))
    await db_session.commit()

    writer = async_sessionmaker(db_engine)()
    rogue_values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "organisation_id": org,
        "key": "race.key",
        "subject_type": "eval",
        "subject_id": "race-subject",
        "value": None,
        "producer_type": "eval",
        "observed_at": _utc_days_ago(100),
        "created_at": _utc_days_ago(100),
    }
    statement = insert(Evidence).values(**rogue_values)
    orig_begin_nested = db_session.begin_nested

    def _hook() -> _ConcurrentInsertSavepoint:
        return _ConcurrentInsertSavepoint(orig_begin_nested(), writer, statement)

    monkeypatch.setattr(db_session, "begin_nested", _hook, raising=False)

    try:
        policy = EvidenceRetentionPolicy(max_age_days=90, lock_timeout_seconds=5.0)
        result = await purge_evidence(db_session, org, policy)
        # The concurrent writer commits only after the sweep has finished —
        # the row was inserted mid-window but never visible to the sweep.
        await writer.commit()
        # Unhook before the follow-up sweep: its own savepoint must not fire
        # another writer INSERT (same id → PK violation).
        monkeypatch.undo()
    finally:
        await writer.close()

    assert result.rows_deleted == 1
    assert await _org_keys(db_session, org) == {"race.key"}

    # Retention is eventually consistent: the next sweep reclaims the row.
    followup = await purge_evidence(db_session, org, EvidenceRetentionPolicy(max_age_days=90))

    assert followup.rows_deleted == 1
    assert await _org_row_count(db_session, org) == 0


# ---------------------------------------------------------------------------
# Observability on real Postgres (§2.8 criterion 5, log half)
# ---------------------------------------------------------------------------


async def test_batch_log_event_carries_policy_fields(
    db_session: AsyncSession, org: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    old = _utc_days_ago(120)
    for i in range(3):
        _add_evidence(db_session, org, f"row.{i}", created_at=old)
    await db_session.commit()
    policy = EvidenceRetentionPolicy(max_age_days=90, max_rows=None)

    with caplog.at_level(logging.INFO, logger=_RETENTION_LOGGER):
        result = await purge_evidence(db_session, org, policy)

    assert result.rows_deleted == 3
    batch_records = [record for record in caplog.records if record.msg == "evidence.retention.batch_deleted"]
    assert len(batch_records) == 1
    record = batch_records[0]
    assert record.rows_deleted == 3
    assert record.batch_size == policy.batch_size
    assert record.max_age_days == 90
    assert record.max_rows is None
