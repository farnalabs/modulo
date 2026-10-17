"""FAR-1856 / FAR-1857 — housekeeping scanner + cleanup counting on real Postgres.

Two defects SQLite cannot reproduce, so both are pinned here against the real
migrated schema (testcontainers ``postgres:16-alpine``):

* **FAR-1856** — ``_scan_empty_lifecycle_maps`` builds
  ``LifecycleMap.content_json == {}``. The model declared a bare generic
  ``JSON`` while migration ``0147_json_to_jsonb_standardize`` converted the
  column to ``jsonb``, so Postgres rejected the predicate with
  ``operator does not exist: jsonb = json``. ``scan_all`` tolerates an
  exception per-scanner, so the category reported zero candidates forever
  while the run looked healthy — a silent failure path. The model now carries
  the repository's ``JSON().with_variant(JSONB(), "postgresql")`` convention
  (the ``agent.py`` / ``audit_event.py`` precedent) so the parameter binds as
  ``jsonb``.

* **FAR-1857** — ``perform_cleanup`` incremented ``deleted_count`` at
  ``session.delete()`` time, before the per-group savepoint flushed. The flush
  happens when the savepoint context exits, so a row later refused by an
  ``ON DELETE RESTRICT`` FK rolled back with the savepoint yet stayed counted:
  a destructive operation over-reported success. The count now only advances
  after the savepoint has released successfully.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.core.housekeeping import _scan_empty_lifecycle_maps, scan_all
from modulo.db.models.account import Account
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.secret import Secret

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """System-admin JWT — every housekeeping route is system-admin gated."""
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        is_system_admin=True,
        client_kind="browser",
    )


async def _seed_org_and_account(session: AsyncSession, *, slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Org + account seed inside the caller's transaction (no commit here)."""
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    session.add(Organisation(id=org_id, name="Housekeeping Test Org", slug=f"{slug_prefix}-{org_id.hex[:8]}"))
    session.add(
        Account(
            id=account_id,
            email=f"{slug_prefix}-{account_id.hex[:8]}@test.local",
            display_name="Housekeeping Test User",
            auth_provider="local",
        )
    )
    await session.flush()
    return org_id, account_id


class TestEmptyLifecycleMapScanOnPostgres:
    """FAR-1856: emptiness is structural ("no stages"), not ``content_json == {}``.

    Two defects lived here: the bare-``JSON`` model made ``== {}`` raise
    ``operator does not exist: jsonb = json`` on Postgres, and even once that
    compiled, an equality against ``{}`` never matched the three-key shape the
    UI persists (``{"stages": [], "edges": [], "notes": ""}``), so an emptied
    map was silently never surfaced.
    """

    async def test_empty_maps_are_surfaced_and_staged_map_is_not(
        self,
        db_session: AsyncSession,
    ) -> None:
        org_id, account_id = await _seed_org_and_account(db_session, slug_prefix="far1856")
        empty_id = uuid.uuid4()
        ui_emptied_id = uuid.uuid4()
        filled_id = uuid.uuid4()
        db_session.add(
            LifecycleMap(
                id=empty_id,
                organisation_id=org_id,
                account_id=account_id,
                name="hk-empty-map",
                content_json={},
            )
        )
        # The editor's save path writes this three-key shape
        # (``normalize_content``) after all stages are deleted in the UI. It is
        # distinct from the bare ``{}`` above (a map created with no content);
        # both are real shapes the predicate must surface.
        db_session.add(
            LifecycleMap(
                id=ui_emptied_id,
                organisation_id=org_id,
                account_id=account_id,
                name="hk-ui-emptied-map",
                content_json={"stages": [], "edges": [], "notes": ""},
            )
        )
        db_session.add(
            LifecycleMap(
                id=filled_id,
                organisation_id=org_id,
                account_id=account_id,
                name="hk-filled-map",
                content_json={"stages": ["build", "test"], "edges": [], "notes": "n"},
            )
        )
        await db_session.flush()

        # Direct scanner call: pre-fix this RAISED ProgrammingError
        # ("operator does not exist: jsonb = json") instead of returning.
        candidates = await _scan_empty_lifecycle_maps(db_session, org_id)

        surfaced_names = sorted(c.name for c in candidates)
        assert surfaced_names == ["hk-empty-map", "hk-ui-emptied-map"], (
            f"expected both empty-shaped maps, got {surfaced_names}"
        )
        surfaced_ids = {c.id for c in candidates}
        assert str(empty_id) in surfaced_ids
        assert str(ui_emptied_id) in surfaced_ids
        assert str(filled_id) not in surfaced_ids

        # Production path: scan_all swallows a per-scanner exception and
        # substitutes [], so the category itself must carry the candidate —
        # pre-fix it came back empty while the route still reported 200.
        results = await scan_all(db_session, org_id)
        by_category = {r.category: r for r in results}
        category = by_category["empty_lifecycle_maps"]
        category_names = sorted(c.name for c in category.candidates)
        assert category_names == ["hk-empty-map", "hk-ui-emptied-map"], f"scan_all category was {category_names}"


async def _best_effort_cleanup(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Remove the seeded org's rows (the container is per-session anyway).

    Best-effort: a failure here must not mask the test's own result.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    with contextlib.suppress(Exception):
        async with factory() as session, session.begin():
            for statement in (
                "DELETE FROM runs WHERE organisation_id = :oid",
                "DELETE FROM pipeline_snapshots WHERE organisation_id = :oid",
                "DELETE FROM pipelines WHERE organisation_id = :oid",
                "DELETE FROM secrets WHERE organisation_id = :oid",
                "DELETE FROM org_memberships WHERE organisation_id = :oid",
                "DELETE FROM audit_events WHERE organisation_id = :oid",
                "DELETE FROM lifecycle_maps WHERE organisation_id = :oid",
                "DELETE FROM organisations WHERE id = :oid",
                "DELETE FROM accounts WHERE id = :aid",
            ):
                await session.execute(text(statement), {"oid": str(org_id), "aid": str(account_id)})


@dataclass(frozen=True)
class _CleanupBatch:
    org_id: uuid.UUID
    account_id: uuid.UUID
    secret_id: uuid.UUID
    snapshot_id: uuid.UUID


async def _seed_cleanup_batch(db_engine: AsyncEngine, *, slug_prefix: str) -> _CleanupBatch:
    """Seed one deletable row and one FK-RESTRICT-refused row.

    * ``secrets`` — nothing FKs ``secrets.id``, so it deletes cleanly.
    * ``pipeline_snapshots`` — ``runs.snapshot_id`` references it with
      ``ON DELETE RESTRICT`` (migration 0110), so its DELETE is refused at
      flush time (the savepoint release).
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    secret_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()

    async with factory() as session, session.begin():
        session.add(Organisation(id=org_id, name="Housekeeping Cleanup Org", slug=f"{slug_prefix}-{org_id.hex[:8]}"))
        session.add(
            Account(
                id=account_id,
                email=f"{slug_prefix}-{account_id.hex[:8]}@test.local",
                display_name="Housekeeping Cleanup User",
                auth_provider="local",
            )
        )
        # Flush the FK targets first so the dependent rows below cannot be
        # ordered ahead of them by a single big autoflush.
        await session.flush()
        # `get_current_tenant_user` requires a live org membership.
        session.add(OrgMembership(id=uuid.uuid4(), account_id=account_id, organisation_id=org_id, role="admin"))
        session.add(Secret(id=secret_id, organisation_id=org_id, key="hk-cleanup-deletable", encrypted_value=b"x"))
        session.add(Pipeline(id=pipeline_id, organisation_id=org_id, account_id=account_id, name="hk-cleanup-pipeline"))
        session.add(
            PipelineSnapshot(
                id=snapshot_id,
                organisation_id=org_id,
                pipeline_id=pipeline_id,
                snapshot_version=1,
                account_id=account_id,
                graph_json={},
                connector_bindings_json=[],
                schema_pins_json=[],
                prompt_pins_json=[],
                model_backend_pins_json=[],
            )
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, "
                "snapshot_id, trigger_type, status, input_hash, "
                "langgraph_thread_id, run_number) "
                "VALUES (:rid, :oid, :pid, :sid, 'manual', 'pending', "
                ":hash, :tid, :rn)"
            ),
            {
                "rid": str(uuid.uuid4()),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "hash": uuid.uuid4().hex,
                "tid": f"thread-{slug_prefix}-{snapshot_id.hex[:8]}",
                "rn": 1,
            },
        )
    return _CleanupBatch(
        org_id=org_id,
        account_id=account_id,
        secret_id=secret_id,
        snapshot_id=snapshot_id,
    )


class TestPerformCleanupDeletedCountOnPostgres:
    """FAR-1857: ``deleted_count`` must count rows that actually committed."""

    async def test_mixed_batch_counts_only_the_rows_actually_deleted(
        self,
        db_engine: AsyncEngine,
        integration_client: AsyncClient,
    ) -> None:
        batch = await _seed_cleanup_batch(db_engine, slug_prefix="far1857")
        headers = {"Authorization": f"Bearer {_token(batch.org_id, batch.account_id)}"}
        try:
            resp = await integration_client.post(
                "/api/v1/admin/housekeeping/cleanup",
                json={
                    "items": [
                        {"id": str(batch.secret_id), "entity_type": "secret"},
                        {"id": str(batch.snapshot_id), "entity_type": "pipeline_snapshot"},
                    ]
                },
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            body: dict[str, Any] = resp.json()

            # THE FIX: only the row that really committed is counted. The old
            # code counted at ``session.delete()`` time and returned 2 here.
            assert body["deleted_count"] == 1, body
            assert len(body["errors"]) == 1, body
            error = body["errors"][0]
            assert error["id"] == str(batch.snapshot_id), error
            assert error["entity_type"] == "pipeline_snapshot", error
            assert error["error"] == "Foreign key constraint violation", error

            async with db_engine.connect() as conn:
                secret_rows = (
                    await conn.execute(text("SELECT id FROM secrets WHERE id = :sid"), {"sid": str(batch.secret_id)})
                ).all()
                snapshot_rows = (
                    await conn.execute(
                        text("SELECT id FROM pipeline_snapshots WHERE id = :sid"),
                        {"sid": str(batch.snapshot_id)},
                    )
                ).all()

            # The counted row really is gone, the refused row really survived.
            assert len(secret_rows) == 0, "the counted deletion must actually have happened"
            assert len(snapshot_rows) == 1, "the FK-restricted row must have been rolled back"
        finally:
            await _best_effort_cleanup(db_engine, batch.org_id, batch.account_id)


class TestMcpPerformHousekeepingDeletedCountOnPostgres:
    """FAR-1857: the MCP twin counts only committed rows too.

    ``perform_housekeeping`` deletes via
    ``_delete_housekeeping_groups`` -> ``_delete_housekeeping_group``. The
    identical count-inside-the-savepoint defect lived here; this pins the real
    Postgres behaviour so the two call sites cannot drift again.
    """

    async def test_mcp_mixed_batch_counts_only_rows_actually_deleted(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        from modulo.api.mcp_server import _delete_housekeeping_groups
        from modulo.core.housekeeping import ENTITY_MODEL_MAP

        batch = await _seed_cleanup_batch(db_engine, slug_prefix="far1857mcp")
        errors: list[dict[str, str]] = []
        try:
            factory = async_sessionmaker(db_engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                deleted = await _delete_housekeeping_groups(
                    session,
                    {
                        "secret": [str(batch.secret_id)],
                        "pipeline_snapshot": [str(batch.snapshot_id)],
                    },
                    ENTITY_MODEL_MAP,
                    batch.org_id,
                    errors,
                )

            # Only the row that really committed is counted. The old MCP code
            # incremented inside the savepoint and returned 2 here.
            assert deleted == 1
            assert len(errors) == 1, errors
            error = errors[0]
            assert error["id"] == str(batch.snapshot_id), error
            assert error["entity_type"] == "pipeline_snapshot", error
            assert error["error"] == "Foreign key constraint violation", error

            async with db_engine.connect() as conn:
                secret_rows = (
                    await conn.execute(text("SELECT id FROM secrets WHERE id = :sid"), {"sid": str(batch.secret_id)})
                ).all()
                snapshot_rows = (
                    await conn.execute(
                        text("SELECT id FROM pipeline_snapshots WHERE id = :sid"),
                        {"sid": str(batch.snapshot_id)},
                    )
                ).all()

            assert len(secret_rows) == 0, "the counted deletion must actually have happened"
            assert len(snapshot_rows) == 1, "the FK-restricted row must have been rolled back"
        finally:
            await _best_effort_cleanup(db_engine, batch.org_id, batch.account_id)
