"""Evidence retention — policy CRUD and batched purge sweep (FAR-961, chunk 9a).

The retention policy is stored in ``Organisation.settings_json`` under the
``evidence_retention`` key (mirroring the run-retention pattern).  The purge
sweep is the **single sanctioned deletion path** for evidence rows — no other
module may delete from the ``evidence`` table (append-only carve-out, §2.4).

Concurrency: the sweep acquires a per-org ``pg_advisory_lock`` via the shared
``db.repositories.locks.PostgresLock`` service.  A second invocation that
cannot acquire the lock within the configurable timeout logs a warning and
exits cleanly (idempotent).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.evidence import Evidence
from modulo.db.models.organisation import Organisation
from modulo.db.repositories.locks import LockAcquireError, PostgresLock

_log = logging.getLogger(__name__)

# ── Policy defaults ──────────────────────────────────────────────────────

DEFAULT_MAX_AGE_DAYS: int = 90
DEFAULT_BATCH_SIZE: int = 500
DEFAULT_LOCK_TIMEOUT_SECONDS: float = 30.0

# Settings-json key inside ``Organisation.settings_json``.
_POLICY_KEY = "evidence_retention"

# Advisory-lock key namespace — keeps the retention sweep's lock disjoint from
# other per-org locks (e.g. the last-admin guard, which keys on ``str(org_id)``).
_LOCK_KEY_NAMESPACE = "evidence_retention"

# Shared Postgres advisory-lock service (``db.repositories.locks``) — the sweep
# reuses it rather than re-implementing the try-lock / poll / unlock mechanics.
_LOCK_SERVICE = PostgresLock()


def _lock_key_for_org(org_id: Any) -> str:
    """Return the namespaced advisory-lock key for an org's retention sweep."""
    return f"{_LOCK_KEY_NAMESPACE}:{org_id}"


# ── Policy dataclass ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceRetentionPolicy:
    """Retention policy for evidence rows."""

    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    max_rows: int | None = None  # None means unlimited
    batch_size: int = DEFAULT_BATCH_SIZE
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"max_age_days": self.max_age_days}
        if self.max_rows is not None:
            d["max_rows"] = self.max_rows
        if self.batch_size != DEFAULT_BATCH_SIZE:
            d["batch_size"] = self.batch_size
        if self.lock_timeout_seconds != DEFAULT_LOCK_TIMEOUT_SECONDS:
            d["lock_timeout_seconds"] = self.lock_timeout_seconds
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> EvidenceRetentionPolicy:
        if not d or not isinstance(d, dict):
            return cls()
        max_age = d.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
        if not isinstance(max_age, int) or max_age < 1:
            max_age = DEFAULT_MAX_AGE_DAYS
        max_rows_raw = d.get("max_rows")
        max_rows: int | None = None
        if max_rows_raw is not None:
            if isinstance(max_rows_raw, int) and max_rows_raw > 0:
                max_rows = max_rows_raw
            elif isinstance(max_rows_raw, str) and max_rows_raw.isdigit():
                max_rows = int(max_rows_raw)
        batch_size = d.get("batch_size", DEFAULT_BATCH_SIZE)
        if not isinstance(batch_size, int) or batch_size < 1:
            batch_size = DEFAULT_BATCH_SIZE
        lock_timeout = d.get("lock_timeout_seconds", DEFAULT_LOCK_TIMEOUT_SECONDS)
        if not isinstance(lock_timeout, (int, float)) or lock_timeout <= 0:
            lock_timeout = DEFAULT_LOCK_TIMEOUT_SECONDS
        return cls(
            max_age_days=max_age,
            max_rows=max_rows,
            batch_size=batch_size,
            lock_timeout_seconds=float(lock_timeout),
        )


# ── Policy CRUD (reads/writes Organisation.settings_json) ────────────────


async def load_policy(session: AsyncSession, org_id: Any) -> EvidenceRetentionPolicy:
    """Load the evidence retention policy for an org."""
    result = await session.execute(select(Organisation.settings_json).where(Organisation.id == org_id).limit(1))
    row = result.scalar_one_or_none()
    if isinstance(row, dict):
        return EvidenceRetentionPolicy.from_dict(row.get(_POLICY_KEY))
    return EvidenceRetentionPolicy()


def _merge_org_setting(org: Organisation, key: str, value: object) -> None:
    """Merge *value* into ``org.settings_json`` under *key* without dropping other keys."""
    settings: dict[str, Any] = dict(org.settings_json) if org.settings_json else {}
    settings[key] = value
    org.settings_json = settings


async def save_policy(
    session: AsyncSession,
    org_id: Any,
    policy: EvidenceRetentionPolicy,
) -> None:
    """Persist the evidence retention policy for an org."""
    result = await session.execute(select(Organisation).where(Organisation.id == org_id).limit(1))
    org = result.scalar_one_or_none()
    if org is None:
        raise ValueError(f"Organisation {org_id} not found")
    _merge_org_setting(org, _POLICY_KEY, policy.to_dict())
    await session.flush()


# ── Purge sweep ──────────────────────────────────────────────────────────


@dataclass
class PurgeResult:
    """Result of an evidence retention purge sweep."""

    rows_deleted: int = 0
    batches: int = 0
    max_age_days: int = 0
    max_rows: int | None = None


def _record_deletion_metrics(org_id: Any, rows_deleted: int) -> None:
    """Emit deletion count via OTel counter (no-op when no meter is wired).

    The counter name ``modulo_evidence_retention_deletions_total`` matches
    the Prometheus exposition name required by the spec (§2.8, criterion 5).
    """
    try:
        from opentelemetry import metrics

        provider = metrics.get_meter_provider()
        if provider is None:
            return
        meter = provider.get_meter("modulo.evidence_retention", version="0.1.0")
        counter = meter.create_counter(
            name="modulo_evidence_retention_deletions_total",
            description="Evidence rows deleted by retention sweep",
            unit="1",
        )
        counter.add(rows_deleted, {"organisation_id": str(org_id)})
    except Exception:  # noqa: S110
        # Telemetry must never break the sweep.
        pass


async def _delete_batch(
    session: AsyncSession,
    org_id: Any,
    ids: Sequence[Any],
    policy: EvidenceRetentionPolicy,
) -> int:
    """Delete one batch of evidence ids inside a SAVEPOINT and emit metrics.

    The SAVEPOINT keeps the caller's transaction (and its transaction-local
    ``app.organisation_id`` RLS scope) intact — there is no per-batch COMMIT.
    Returns the number of rows deleted.
    """
    async with session.begin_nested():
        await session.execute(delete(Evidence).where(Evidence.id.in_(ids)))

    batch_count = len(ids)

    _log.info(
        "evidence.retention.batch_deleted",
        extra={
            "org_id": str(org_id),
            "batch_size": policy.batch_size,
            "rows_deleted": batch_count,
            "max_age_days": policy.max_age_days,
            "max_rows": policy.max_rows,
        },
    )
    _record_deletion_metrics(org_id, batch_count)

    return batch_count


async def purge_evidence(
    session: AsyncSession,
    org_id: Any,
    policy: EvidenceRetentionPolicy | None = None,
) -> PurgeResult:
    """Execute the evidence retention purge sweep for an org.

    This is the **single sanctioned deletion path** for evidence rows
    (append-only carve-out, §2.4).  No other module may delete from the
    ``evidence`` table.

    The sweep:
    1. Acquires a per-org advisory lock (idempotent — second invoker exits).
    2. Iterates in batches: selects oldest evidence rows older than
       ``max_age_days``, then deletes each batch inside a SAVEPOINT **within
       the caller's transaction**.  It deliberately never commits mid-sweep:
       a COMMIT would end the caller's transaction and discard the
       transaction-local ``app.organisation_id`` RLS GUC, so every batch
       after the first would see no tenant scope (the run-retention sweep
       uses the same single-transaction shape).
    3. When ``max_rows`` is set, also deletes oldest rows exceeding the
       count limit (after the age purge).
    4. Emits a structured log event and an OTel counter per batch.
    """
    if policy is None:
        policy = await load_policy(session, org_id)

    # Acquire the shared per-org advisory lock (idempotent — a second
    # invocation that cannot acquire it within the timeout exits cleanly).
    lock_key = _lock_key_for_org(org_id)
    try:
        await _LOCK_SERVICE.acquire_lock(session, lock_key, lock_timeout=policy.lock_timeout_seconds)
    except LockAcquireError:
        _log.warning(
            "evidence.retention.lock_timeout",
            extra={"org_id": str(org_id), "timeout_seconds": policy.lock_timeout_seconds},
        )
        return PurgeResult(max_age_days=policy.max_age_days, max_rows=policy.max_rows)

    total_deleted = 0
    batches = 0
    cutoff = datetime.now(UTC) - timedelta(days=policy.max_age_days)

    try:
        # Phase 1: age-based purge
        while True:
            # Select oldest eligible rows for the sweep's fixed cutoff. Rows
            # written after the sweep began fall outside the cutoff and are
            # left for the next sweep.
            result = await session.execute(
                select(Evidence.id)
                .where(
                    Evidence.organisation_id == org_id,
                    Evidence.created_at < cutoff,
                )
                .order_by(Evidence.created_at, Evidence.id)
                .limit(policy.batch_size)
            )
            ids = result.scalars().all()
            if not ids:
                break

            # Delete within a SAVEPOINT — the caller's transaction (and its
            # transaction-local RLS scope) is preserved; there is no per-batch
            # COMMIT.
            total_deleted += await _delete_batch(session, org_id, ids, policy)
            batches += 1

        # Phase 2: row-count purge (if max_rows is set)
        if policy.max_rows is not None:
            while True:
                # Count current rows for this org.
                count_result = await session.execute(
                    select(func.count()).select_from(Evidence).where(Evidence.organisation_id == org_id)
                )
                current_count = count_result.scalar() or 0
                if current_count <= policy.max_rows:
                    break

                # Delete the oldest rows that exceed the limit.
                excess = current_count - policy.max_rows
                delete_count = min(excess, policy.batch_size)

                result = await session.execute(
                    select(Evidence.id)
                    .where(Evidence.organisation_id == org_id)
                    .order_by(Evidence.created_at, Evidence.id)
                    .limit(delete_count)
                )
                ids = result.scalars().all()
                if not ids:
                    break

                # Same single-transaction contract as Phase 1: SAVEPOINT, no
                # per-batch COMMIT.
                total_deleted += await _delete_batch(session, org_id, ids, policy)
                batches += 1

    finally:
        await _LOCK_SERVICE.release_lock(session, lock_key)

    return PurgeResult(
        rows_deleted=total_deleted,
        batches=batches,
        max_age_days=policy.max_age_days,
        max_rows=policy.max_rows,
    )


# ── Evidence row count helper ────────────────────────────────────────────


async def count_evidence_rows(session: AsyncSession, org_id: Any) -> int:
    """Return the total evidence row count for an org."""
    result = await session.execute(select(func.count()).select_from(Evidence).where(Evidence.organisation_id == org_id))
    return result.scalar() or 0
