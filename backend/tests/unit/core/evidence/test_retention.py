"""Evidence retention — acceptance tests (FAR-961, chunk 9a §2.5/§2.8).

In-memory SQLite (aiosqlite) coverage of the policy CRUD, the batched purge
sweep mechanics, the advisory-lock timeout exit, and the observability
contract.  ``pg_advisory_lock`` is Postgres-only: here the lock boundary is
mocked and the polling/timeout logic of ``_acquire_advisory_lock`` is
exercised against a stub session.  The REAL advisory-lock behaviour (mutual
exclusion between two sweeps) is covered in
``tests/integration/db/test_evidence_retention.py``.

Batch-window insert safety: a row written between a batch's SELECT and its
DELETE is never in that batch's id list.  For the realistic case (a normal
row's ``created_at`` is its insert time, so it is younger than the cutoff)
the row survives the whole sweep — asserted here.  The back-dated
``created_at`` variant is deterministic only with a second connection and is
covered in the integration file.

Spec criteria covered here: 1 and 2 (mechanics), 5 (log event + counter),
9 (timeout exit), 10 (window safety, unit half).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opentelemetry import metrics as otel_metrics
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import evidence_retention
from modulo.core.evidence_retention import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    DEFAULT_MAX_AGE_DAYS,
    EvidenceRetentionPolicy,
    _acquire_advisory_lock,
    _record_deletion_metrics,
    count_evidence_rows,
    load_policy,
    purge_evidence,
    save_policy,
)
from modulo.db.models import Base
from modulo.db.models.evidence import Evidence
from modulo.db.models.organisation import Organisation

_RETENTION_LOGGER = "modulo.core.evidence_retention"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _tables() -> list[Any]:
    return [Evidence.__table__, Organisation.__table__]


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    active = maker()
    yield active
    await active.close()
    await engine.dispose()


def _utc_days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _evidence(
    org_id: uuid.UUID,
    key: str,
    *,
    created_at: datetime | None = None,
    value: Any = True,
    subject_id: str = "subject-1",
) -> Evidence:
    stamp = created_at or datetime.now(UTC)
    return Evidence(
        organisation_id=org_id,
        key=key,
        subject_type="eval",
        subject_id=subject_id,
        value=value,
        observed_at=stamp,
        producer_type="eval",
        created_at=stamp,
    )


async def _seed_org(session: AsyncSession) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Organisation(id=org_id, name="ret-org", slug=f"ret-{org_id.hex[:8]}"))
    await session.commit()
    return org_id


def _grant_locks(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    """Replace the Postgres advisory-lock boundary with mocks (SQLite has no pg locks)."""
    acquire = AsyncMock(return_value=True)
    release = AsyncMock()
    monkeypatch.setattr(evidence_retention, "_acquire_advisory_lock", acquire)
    monkeypatch.setattr(evidence_retention, "_release_advisory_lock", release)
    return acquire, release


class _WindowSavepoint:
    """Wrap ``session.begin_nested()`` and run a hook inside the batch window.

    The hook fires after the batch SELECT has fixed the deletion id list and
    before the DELETE executes — exactly the concurrency window the spec's
    batch-safety guarantee is about.
    """

    def __init__(self, inner: Any, hook: Any) -> None:
        self._inner = inner
        self._hook = hook

    async def __aenter__(self) -> Any:
        await self._hook()
        return await self._inner.__aenter__()

    async def __aexit__(self, *exc_info: object) -> Any:
        return await self._inner.__aexit__(*exc_info)


# ---------------------------------------------------------------------------
# Policy dataclass (§2.6)
# ---------------------------------------------------------------------------


def test_policy_defaults_match_module_constants() -> None:
    policy = EvidenceRetentionPolicy()

    assert policy.max_age_days == DEFAULT_MAX_AGE_DAYS
    assert policy.batch_size == DEFAULT_BATCH_SIZE
    assert policy.lock_timeout_seconds == DEFAULT_LOCK_TIMEOUT_SECONDS
    assert policy.max_rows is None


def test_policy_to_dict_omits_default_fields() -> None:
    settings = EvidenceRetentionPolicy().to_dict()

    assert settings == {"max_age_days": DEFAULT_MAX_AGE_DAYS}


def test_policy_roundtrip_preserves_all_fields() -> None:
    policy = EvidenceRetentionPolicy(max_age_days=7, max_rows=1000, batch_size=50, lock_timeout_seconds=1.5)

    assert EvidenceRetentionPolicy.from_dict(policy.to_dict()) == policy


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, EvidenceRetentionPolicy()),
        ({"max_age_days": 0}, EvidenceRetentionPolicy()),
        ({"max_age_days": "30"}, EvidenceRetentionPolicy()),
        ({"max_rows": "50"}, EvidenceRetentionPolicy(max_rows=50)),
        ({"max_rows": -5}, EvidenceRetentionPolicy()),
        ({"batch_size": 0}, EvidenceRetentionPolicy()),
        ({"lock_timeout_seconds": -1}, EvidenceRetentionPolicy()),
    ],
)
def test_policy_from_dict_clamps_invalid_values(raw: dict[str, Any], expected: EvidenceRetentionPolicy) -> None:
    assert EvidenceRetentionPolicy.from_dict(raw) == expected


# ---------------------------------------------------------------------------
# Policy CRUD (§2.6)
# ---------------------------------------------------------------------------


async def test_load_policy_defaults_when_org_missing(session: AsyncSession) -> None:
    policy = await load_policy(session, uuid.uuid4())

    assert policy == EvidenceRetentionPolicy()


async def test_save_policy_roundtrip_preserves_sibling_settings(session: AsyncSession) -> None:
    org_id = await _seed_org(session)
    result = await session.execute(select(Organisation).where(Organisation.id == org_id))
    org = result.scalar_one()
    org.settings_json = {"theme": "dark"}
    await session.commit()

    await save_policy(session, org_id, EvidenceRetentionPolicy(max_age_days=14, max_rows=200))

    loaded = await load_policy(session, org_id)
    assert loaded == EvidenceRetentionPolicy(max_age_days=14, max_rows=200)
    result = await session.execute(select(Organisation.settings_json).where(Organisation.id == org_id))
    settings = result.scalar_one()
    assert settings["theme"] == "dark"
    assert "evidence_retention" in settings


async def test_save_policy_missing_org_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="not found"):
        await save_policy(session, uuid.uuid4(), EvidenceRetentionPolicy())


# ---------------------------------------------------------------------------
# Purge sweep mechanics (§2.5 criteria 1, 2)
# ---------------------------------------------------------------------------


async def test_age_purge_deletes_old_rows_and_keeps_recent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = await _seed_org(session)
    session.add(_evidence(org_id, "old.key", created_at=_utc_days_ago(100)))
    session.add(_evidence(org_id, "recent.key", created_at=_utc_days_ago(30)))
    await session.commit()
    _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy(max_age_days=90))

    assert result.rows_deleted == 1
    assert result.batches == 1
    assert result.max_age_days == 90
    keys = set((await session.execute(select(Evidence.key))).scalars().all())
    assert keys == {"recent.key"}


async def test_max_rows_purge_deletes_oldest_excess(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = await _seed_org(session)
    now = datetime.now(UTC)
    rows = [_evidence(org_id, f"row.{i}", created_at=now - timedelta(minutes=i)) for i in range(120)]
    session.add_all(rows)
    await session.flush()
    # rows[i] has created_at = now - i minutes, so the 20 OLDEST are rows[100:].
    oldest_ids = {row.id for row in rows[100:]}
    await session.commit()
    _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy(max_rows=100))

    assert result.rows_deleted == 20
    assert result.max_rows == 100
    remaining_ids = set((await session.execute(select(Evidence.id))).scalars().all())
    assert remaining_ids.isdisjoint(oldest_ids)
    assert len(remaining_ids) == 100


async def test_max_rows_none_never_triggers_count_purge(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = await _seed_org(session)
    session.add_all([_evidence(org_id, f"row.{i}") for i in range(5)])
    await session.commit()
    acquire, _release = _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy())

    assert result.rows_deleted == 0
    assert result.batches == 0
    assert await count_evidence_rows(session, org_id) == 5
    acquire.assert_awaited_once()


async def test_purge_batches_oldest_first(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = await _seed_org(session)
    old = _utc_days_ago(120)
    session.add_all([_evidence(org_id, f"row.{i}", created_at=old - timedelta(minutes=i)) for i in range(7)])
    await session.commit()
    _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy(max_age_days=90, batch_size=3))

    assert result.rows_deleted == 7
    assert result.batches == 3
    assert await count_evidence_rows(session, org_id) == 0


async def test_purge_is_org_scoped(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    org_a = await _seed_org(session)
    org_b = uuid.uuid4()
    session.add(Organisation(id=org_b, name="ret-org-b", slug=f"ret-b-{org_b.hex[:8]}"))
    session.add(_evidence(org_a, "a.key", created_at=_utc_days_ago(200)))
    session.add(_evidence(org_b, "b.key", created_at=_utc_days_ago(200)))
    await session.commit()
    _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_a, EvidenceRetentionPolicy(max_age_days=90))

    assert result.rows_deleted == 1
    remaining_orgs = set((await session.execute(select(Evidence.organisation_id))).scalars().all())
    assert remaining_orgs == {org_b}


# ---------------------------------------------------------------------------
# Batch-window insert safety (§2.5 criterion 10, unit half)
# ---------------------------------------------------------------------------


async def test_row_inserted_during_batch_window_survives(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = await _seed_org(session)
    session.add(_evidence(org_id, "old.key", created_at=_utc_days_ago(100)))
    await session.commit()

    rogue = _evidence(org_id, "fresh.key", created_at=datetime.now(UTC))
    orig_begin_nested = session.begin_nested

    async def _plant_row() -> None:
        session.add(rogue)
        await session.flush()

    def _hook() -> _WindowSavepoint:
        return _WindowSavepoint(orig_begin_nested(), _plant_row)

    monkeypatch.setattr(session, "begin_nested", _hook, raising=False)
    _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy(max_age_days=90))

    assert result.rows_deleted == 1
    keys = set((await session.execute(select(Evidence.key))).scalars().all())
    assert keys == {"fresh.key"}


# ---------------------------------------------------------------------------
# Advisory-lock timeout exit (§2.5 criterion 9, unit half)
# ---------------------------------------------------------------------------


async def test_lock_timeout_exits_cleanly_without_deletions(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    org_id = await _seed_org(session)
    session.add(_evidence(org_id, "old.key", created_at=_utc_days_ago(100)))
    await session.commit()
    acquire = AsyncMock(return_value=False)
    release = AsyncMock()
    monkeypatch.setattr(evidence_retention, "_acquire_advisory_lock", acquire)
    monkeypatch.setattr(evidence_retention, "_release_advisory_lock", release)

    with caplog.at_level(logging.WARNING, logger=_RETENTION_LOGGER):
        result = await purge_evidence(session, org_id, EvidenceRetentionPolicy(lock_timeout_seconds=0.2))

    assert result.rows_deleted == 0
    assert result.batches == 0
    assert await count_evidence_rows(session, org_id) == 1
    timeout_records = [record for record in caplog.records if record.msg == "evidence.retention.lock_timeout"]
    assert len(timeout_records) == 1
    release.assert_not_awaited()


async def test_purge_releases_advisory_lock_after_sweep(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = await _seed_org(session)
    acquire, release = _grant_locks(monkeypatch)

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy())

    acquire.assert_awaited_once()
    release.assert_awaited_once()
    assert result.rows_deleted == 0


class _StubResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _StubSession:
    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = list(outcomes)
        self.execute_calls = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> _StubResult:
        self.execute_calls += 1
        return _StubResult(self._outcomes.pop(0))


async def test_acquire_advisory_lock_retries_until_granted() -> None:
    stub = _StubSession([False, False, True])

    acquired = await _acquire_advisory_lock(stub, uuid.uuid4(), lock_timeout=5.0)

    assert acquired is True
    assert stub.execute_calls == 3


async def test_acquire_advisory_lock_times_out_without_grant() -> None:
    stub = _StubSession([False] * 10)

    acquired = await _acquire_advisory_lock(stub, uuid.uuid4(), lock_timeout=0.12)

    assert acquired is False
    assert stub.execute_calls >= 2


# ---------------------------------------------------------------------------
# Observability (§2.8 criterion 5)
# ---------------------------------------------------------------------------


async def test_batch_log_event_carries_policy_fields(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    org_id = await _seed_org(session)
    old = _utc_days_ago(120)
    session.add_all([_evidence(org_id, f"row.{i}", created_at=old) for i in range(3)])
    await session.commit()
    _grant_locks(monkeypatch)
    policy = EvidenceRetentionPolicy(max_age_days=90, max_rows=None)

    with caplog.at_level(logging.INFO, logger=_RETENTION_LOGGER):
        result = await purge_evidence(session, org_id, policy)

    assert result.rows_deleted == 3
    batch_records = [record for record in caplog.records if record.msg == "evidence.retention.batch_deleted"]
    assert len(batch_records) == 1
    record = batch_records[0]
    assert record.rows_deleted == 3
    assert record.batch_size == policy.batch_size
    assert record.max_age_days == 90
    assert record.max_rows is None
    assert record.org_id == str(org_id)


class _RecordingCounter:
    def __init__(self, name: str, sink: list[tuple[str, int, dict[str, str] | None]]) -> None:
        self._name = name
        self._sink = sink

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self._sink.append((self._name, amount, attributes))


class _RecordingMeter:
    def __init__(self, sink: list[tuple[str, int, dict[str, str] | None]]) -> None:
        self._sink = sink

    def create_counter(self, name: str, **_kwargs: Any) -> _RecordingCounter:
        return _RecordingCounter(name, self._sink)


class _RecordingProvider:
    def __init__(self, sink: list[tuple[str, int, dict[str, str] | None]]) -> None:
        self._sink = sink

    def get_meter(self, *_args: Any, **_kwargs: Any) -> _RecordingMeter:
        return _RecordingMeter(self._sink)


async def test_deletion_counter_emitted_with_org_attribute(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = await _seed_org(session)
    session.add_all([_evidence(org_id, f"row.{i}", created_at=_utc_days_ago(120)) for i in range(2)])
    await session.commit()
    _grant_locks(monkeypatch)
    sink: list[tuple[str, int, dict[str, str] | None]] = []
    monkeypatch.setattr(otel_metrics, "get_meter_provider", lambda: _RecordingProvider(sink))

    result = await purge_evidence(session, org_id, EvidenceRetentionPolicy(max_age_days=90))

    assert result.rows_deleted == 2
    counter_entries = [entry for entry in sink if entry[0] == "modulo_evidence_retention_deletions_total"]
    assert len(counter_entries) == 1
    _name, amount, attributes = counter_entries[0]
    assert amount == 2
    assert attributes == {"organisation_id": str(org_id)}


def test_metrics_recording_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode() -> Any:
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(otel_metrics, "get_meter_provider", _explode)

    assert _record_deletion_metrics(uuid.uuid4(), 5) is None


# ---------------------------------------------------------------------------
# F1: save_policy raises ValueError for missing org → route maps to 404
# ---------------------------------------------------------------------------


async def test_save_policy_missing_org_raises_value_error(session: AsyncSession) -> None:
    """save_policy raises ValueError when the org doesn't exist.

    The route (admin_evidence_retention) must catch this and map to 404,
    not 500.  This test verifies the ValueError contract that the route
    relies on.
    """
    with pytest.raises(ValueError, match="not found"):
        await save_policy(session, uuid.uuid4(), EvidenceRetentionPolicy())
