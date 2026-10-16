"""FAR-1815: the edit-snapshot retention sweep against REAL Postgres.

The selection unit tests (``tests/unit/core/test_edit_snapshot_retention.py``,
``test_housekeeping_scanners.py``) run on in-memory SQLite; this module proves
the DELETION leg — the real housekeeping cleanup route handler
(``perform_cleanup``) deleting exactly the selected rows — on the production
engine and dialect (asyncpg), with the real migration-built schema so the
``version_kind`` / ``created_kind`` CHECK constraints validate the seeded
vocabulary too.

Without the FAR-1815 scanner the ``edit_snapshot_retention`` category does not
exist: nothing is selected, nothing is deleted, and every removal assertion
below fails. The survivors that MUST remain are asserted by row identity:
the rollback (v1), the initial (v208), the run-frozen snapshot (v209), the
pipeline's latest snapshot (v210), and the kept newest 200 edit versions.

The reference-safety test (FAR-1815 review C1/M1) additionally proves, on the
production engine, that a release-channel-tagged snapshot inside the would-be
delete band survives (never selected), and that a run-referenced snapshot in
the delete band makes the REAL cleanup fail loudly via the ``runs.snapshot_id``
RESTRICT FK (IntegrityError → per-item error) — observed, not merely asserted.
The trigger-pin test pins the Python-canonicalised exclusion (``uuid.UUID``)
of verbatim non-canonical stored pins (uppercase / braced) on the production
jsonb/asyncpg dialect.
"""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.routes.admin_housekeeping import CleanupItem, CleanupRequest, perform_cleanup
from modulo.auth.jwt import TenantPrincipal
from modulo.core.housekeeping import scan_all
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.trigger import Trigger

pytestmark = pytest.mark.integration

_CATEGORY = "edit_snapshot_retention"
# v2..v207 are 206 plain edits, v210 is another plain edit (the latest): the
# provenance-edit pool is 207 rows, so retention keeps the newest 200
# (v210, v207..v9) and deletes exactly v2..v8.
_EXPECTED_DELETED_VERSIONS = {2, 3, 4, 5, 6, 7, 8}
# run / rollback / initial snapshots plus the pipeline's latest snapshot.
_SURVIVOR_VERSIONS = (1, 208, 209, 210)


def _principal(org_id: uuid.UUID, account_id: uuid.UUID) -> TenantPrincipal:
    return TenantPrincipal(
        username="admin",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
        is_system_admin=True,
    )


async def _seed_history(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    account_id: uuid.UUID,
) -> dict[int, uuid.UUID]:
    """Seed one pipeline's full snapshot history (own org — fully isolated).

    Layout: v1 rollback, v2..v207 plain edits (206 rows), v208 initial,
    v209 run, v210 plain edit = the pipeline's latest snapshot overall.
    """
    session.add(Organisation(id=org_id, name="FAR-1815 Sweep Org", slug=f"far-1815-{org_id.hex[:8]}"))
    session.add(Pipeline(id=pipeline_id, organisation_id=org_id, name="far-1815-sweep", account_id=account_id))

    def _snap(version: int, version_kind: str, created_kind: str) -> PipelineSnapshot:
        return PipelineSnapshot(
            id=uuid.uuid4(),
            organisation_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_version=version,
            account_id=account_id,
            graph_json={},
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
            version_kind=version_kind,
            created_kind=created_kind,
        )

    rows: list[PipelineSnapshot] = [
        _snap(1, "edit", "rollback"),
        _snap(208, "edit", "initial"),
        _snap(209, "run", "run"),
        _snap(210, "edit", "edit"),
    ]
    rows.extend(_snap(version, "edit", "edit") for version in range(2, 208))
    session.add_all(rows)
    await session.flush()
    by_version = {row.snapshot_version: row.id for row in rows}
    await session.commit()
    return by_version


async def _cleanup(session: AsyncSession, *, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> None:
    """Remove this test's own rows (runs -> snapshots -> pipeline -> organisation).

    Runs go first: ``runs.snapshot_id`` is ``ondelete=RESTRICT``, so a
    snapshot delete while a run still references it is refused by Postgres.
    """
    await session.rollback()  # clear any failed transaction state first
    await session.execute(
        text("DELETE FROM runs WHERE pipeline_id = :pid"),
        {"pid": str(pipeline_id)},
    )
    await session.execute(
        text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
        {"pid": str(pipeline_id)},
    )
    await session.execute(text("DELETE FROM pipelines WHERE id = :pid"), {"pid": str(pipeline_id)})
    await session.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(org_id)})
    await session.commit()


async def test_retention_sweep_deletes_only_excess_edit_snapshots_on_postgres(
    db_session: AsyncSession,
    test_user: uuid.UUID,
) -> None:
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    try:
        by_version = await _seed_history(db_session, org_id=org_id, pipeline_id=pipeline_id, account_id=test_user)

        # 1. Enumerate through the production scan surface (REST/MCP use this
        # exact scan_all): the category is discoverable and selects exactly the
        # rows beyond the newest 200 provenance-edit snapshots.
        results = await scan_all(db_session, org_id)
        by_category = {r.category: r for r in results}
        assert _CATEGORY in by_category
        result = by_category[_CATEGORY]
        candidate_ids = [c.id for c in result.candidates]
        assert set(candidate_ids) == {str(by_version[version]) for version in _EXPECTED_DELETED_VERSIONS}

        # 2. The REAL cleanup route handler on the production dialect. (The
        # commit closes scan_all's read transaction: perform_cleanup opens its
        # own explicit session.begin().)
        await db_session.commit()
        principal = _principal(org_id, test_user)
        response = await perform_cleanup(
            CleanupRequest(items=[CleanupItem(id=cid, entity_type="pipeline_snapshot") for cid in candidate_ids]),
            db_session,
            principal,
            principal,
        )
        assert response.deleted_count == len(_EXPECTED_DELETED_VERSIONS)
        assert not response.errors

        # 3. Row identities: exactly the selected rows are gone, every
        # survivor is still present.
        remaining = {
            str(snapshot_id)
            for snapshot_id in (
                await db_session.execute(
                    select(PipelineSnapshot.id).where(PipelineSnapshot.pipeline_id == pipeline_id),
                )
            ).scalars()
        }
        expected_remaining = {str(snapshot_id) for snapshot_id in by_version.values()} - set(candidate_ids)
        assert remaining == expected_remaining
        for version in _SURVIVOR_VERSIONS:
            assert str(by_version[version]) in remaining

        # 4. Steady state: a second enumeration finds nothing left to sweep.
        second_results = await scan_all(db_session, org_id)
        second_by_category = {r.category: r for r in second_results}
        assert not second_by_category[_CATEGORY].candidates
    finally:
        await _cleanup(db_session, org_id=org_id, pipeline_id=pipeline_id)


async def test_channel_tagged_and_run_referenced_rows_shape_the_delete_set(
    db_session: AsyncSession,
    test_user: uuid.UUID,
) -> None:
    """FAR-1815 review C1/M1 on real Postgres.

    * C1: a ``channel='canary'`` snapshot inside the would-be delete band is
      routing state (``resolve_snapshot_for_channel``), never a candidate —
      asserted by row identity after the real cleanup ran.
    * run-safety: an edit/edit snapshot inside the delete band that a Run
      references via the ``runs.snapshot_id`` RESTRICT FK is still surfaced
      (selection does not exclude it), and deleting it through the REAL
      cleanup handler fails LOUDLY — observed as a per-item
      IntegrityError/"Foreign key constraint violation" error entry with the
      row surviving, never a silent deletion.
    """
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    try:
        by_version = await _seed_history(db_session, org_id=org_id, pipeline_id=pipeline_id, account_id=test_user)

        # C1: v5 would sit at rank 204 of the unfiltered pool (delete band
        # v2..v8) — tag it for the canary channel instead.
        canary = await db_session.get(PipelineSnapshot, by_version[5])
        assert canary is not None
        canary.channel = "canary"
        # run-safety: v6 is a plain edit/edit row a run pinned at start.
        db_session.add(
            Run(
                id=uuid.uuid4(),
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_id=by_version[6],
                trigger_type="manual",
                status="complete",
                run_number=1,
                input_hash="b" * 64,
                langgraph_thread_id=f"{org_id}:{uuid.uuid4()}",
            )
        )
        await db_session.commit()

        # 1. Selection: the canary row is never surfaced; the pool shrinks by
        # it (206 rows), so exactly the six oldest remaining plain edits are
        # candidates — including the run-referenced v6, whose protection is
        # the RESTRICT FK at delete time, not the selection.
        results = await scan_all(db_session, org_id)
        result = {r.category: r for r in results}[_CATEGORY]
        candidate_ids = {c.id for c in result.candidates}
        assert candidate_ids == {str(by_version[version]) for version in (2, 3, 4, 6, 7, 8)}
        assert str(by_version[5]) not in candidate_ids

        # 2. The REAL cleanup: the run-referenced row fails loudly
        # (savepoint-rolled-back IntegrityError → error entry). The row
        # identities below — not ``deleted_count`` — prove what was actually
        # removed: the counter increments on ``session.delete()`` before the
        # savepoint flush, so it counts the RESTRICT-refused item too
        # (pre-existing perform_cleanup behaviour, out of scope here).
        await db_session.commit()
        principal = _principal(org_id, test_user)
        response = await perform_cleanup(
            CleanupRequest(items=[CleanupItem(id=cid, entity_type="pipeline_snapshot") for cid in candidate_ids]),
            db_session,
            principal,
            principal,
        )
        assert len(response.errors) == 1
        assert response.errors[0]["id"] == str(by_version[6])
        assert "Foreign key constraint violation" in response.errors[0]["error"]

        # 3. Row identities: the run-referenced row and the canary row both
        # survive; only the five unprotected rows are gone.
        remaining = {
            str(snapshot_id)
            for snapshot_id in (
                await db_session.execute(
                    select(PipelineSnapshot.id).where(PipelineSnapshot.pipeline_id == pipeline_id),
                )
            ).scalars()
        }
        assert str(by_version[5]) in remaining
        assert str(by_version[6]) in remaining
        assert remaining == set(map(str, by_version.values())) - {
            str(by_version[version]) for version in (2, 3, 4, 7, 8)
        }

        # 4. The RESTRICT failure is a steady-state loop the operator can see:
        # the run-referenced row keeps being surfaced on the next pass instead
        # of being deleted around the FK or silently dropped from the pool.
        second_results = await scan_all(db_session, org_id)
        second_result = {r.category: r for r in second_results}[_CATEGORY]
        assert {c.id for c in second_result.candidates} == {str(by_version[6])}
    finally:
        await _cleanup(db_session, org_id=org_id, pipeline_id=pipeline_id)


async def test_non_canonical_trigger_pins_are_excluded_on_postgres(
    db_session: AsyncSession,
    test_user: uuid.UUID,
) -> None:
    """FAR-1815 re-gate: verbatim non-canonical trigger pins exclude on Postgres.

    REST stores ``config_json.snapshot_id`` verbatim and unvalidated, and the
    fire path accepts anything ``uuid.UUID(str(...))`` accepts — uppercase,
    braced, ``urn:`` forms. The exclusion therefore canonicalises in Python
    (``uuid.UUID``) instead of comparing raw SQL strings; this test pins that
    behaviour on the production dialect (jsonb ``config_json``, asyncpg), the
    path a dash-normalised string comparison used to cover and the one with no
    Postgres coverage before. The pinned rows must both stay out of the
    candidate set and survive the real cleanup.
    """
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    try:
        by_version = await _seed_history(db_session, org_id=org_id, pipeline_id=pipeline_id, account_id=test_user)
        db_session.add_all(
            [
                Trigger(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    pipeline_id=pipeline_id,
                    name="pin-upper",
                    trigger_type="manual",
                    account_id=test_user,
                    config_json={"snapshot_id": str(by_version[5]).upper()},
                ),
                Trigger(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    pipeline_id=pipeline_id,
                    name="pin-braced",
                    trigger_type="manual",
                    account_id=test_user,
                    config_json={"snapshot_id": f"{{{by_version[6]}}}"},
                ),
            ]
        )
        await db_session.commit()

        results = await scan_all(db_session, org_id)
        result = {r.category: r for r in results}[_CATEGORY]
        candidate_ids = {c.id for c in result.candidates}
        assert str(by_version[5]) not in candidate_ids
        assert str(by_version[6]) not in candidate_ids
        # Pool is 205 rows (both pins excluded) → the five oldest remaining
        # plain edits are surfaced, nothing else.
        assert candidate_ids == {str(by_version[version]) for version in (2, 3, 4, 7, 8)}

        # Real cleanup deletes exactly the unpinned band rows; both pinned
        # snapshots survive.
        await db_session.commit()
        principal = _principal(org_id, test_user)
        response = await perform_cleanup(
            CleanupRequest(items=[CleanupItem(id=cid, entity_type="pipeline_snapshot") for cid in candidate_ids]),
            db_session,
            principal,
            principal,
        )
        assert response.deleted_count == 5
        assert not response.errors
        remaining = {
            str(snapshot_id)
            for snapshot_id in (
                await db_session.execute(
                    select(PipelineSnapshot.id).where(PipelineSnapshot.pipeline_id == pipeline_id),
                )
            ).scalars()
        }
        assert str(by_version[5]) in remaining
        assert str(by_version[6]) in remaining
        assert remaining == set(map(str, by_version.values())) - {
            str(by_version[version]) for version in (2, 3, 4, 7, 8)
        }
    finally:
        await _cleanup(db_session, org_id=org_id, pipeline_id=pipeline_id)
