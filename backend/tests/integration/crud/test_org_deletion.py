"""Integration tests for organisation deletion CRUD.

Tests the full deletion workflow: request → soft-delete → confirm → hard-delete,
including token validation, export capture, and run retention cleanup.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = [
    pytest.mark.integration,
]


# ── Helpers ──────────────────────────────────────────────────────────


async def _create_org(db_engine: AsyncEngine, suffix: str = "") -> uuid.UUID:
    org_id = uuid.uuid4()
    slug = f"del-test-{suffix or org_id.hex[:8]}"
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)",
            ),
            {"id": str(org_id), "name": f"Deletion Test {suffix}", "slug": slug},
        )
    return org_id


async def _create_user(db_engine: AsyncEngine, org_id: uuid.UUID, email: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, "
                "auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)",
            ),
            {
                "id": str(account_id),
                "email": email,
                "name": email.split("@", maxsplit=1)[0],
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) "
                "VALUES (:mid, :aid, :oid, 'admin')",
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _create_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
) -> uuid.UUID:
    pid = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "visibility, max_concurrent_runs, lock_wait_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :org_id, :name, :account_id, 'org', 1, 30, '{}'::json, '[]'::json)",
            ),
            {
                "id": str(pid),
                "org_id": str(org_id),
                "name": name,
                "account_id": str(account_id),
            },
        )
    return pid


async def _create_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)",
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _insert_run(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    thread_id: str,
    status: str,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    # run_number must be unique per (org, run_number) — derive it from the
    # unique run_id so parallel/serial tests never collide.
    run_number = int(run_id.int % 10**9) + 1
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, input_hash, langgraph_thread_id, run_number) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :st, :ih, :thread, :rn)",
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "st": status,
                "ih": uuid.uuid4().hex,
                "thread": thread_id,
                "rn": run_number,
            },
        )
    return run_id


async def _count_rows(db_engine: AsyncEngine, table: str, org_id: uuid.UUID | None = None) -> int:
    async with db_engine.connect() as conn:
        if org_id:
            result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE organisation_id = :oid"),  # noqa: S608
                {"oid": str(org_id)},
            )
        else:
            result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))  # noqa: S608
        return result.scalar_one()


async def _get_org_status(db_engine: AsyncEngine, org_id: uuid.UUID) -> dict[str, Any]:
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT status, deleted_at, deletion_token, "
                "deletion_token_expires_at, export_bundle_json "
                "FROM organisations WHERE id = :id",
            ),
            {"id": str(org_id)},
        )
        r = row.one_or_none()
        if r is None:
            return {}
        return {
            "status": r[0],
            "deleted_at": r[1],
            "deletion_token": r[2],
            "deletion_token_expires_at": r[3],
            "export_bundle_json": r[4],
        }


# ── Tests: request_org_deletion ─────────────────────────────────────


class TestRequestOrgDeletion:
    async def test_raises_when_org_not_found(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(ValueError, match="Organisation not found"):
                await request_org_deletion(
                    session,
                    org_id=uuid.uuid4(),
                    _actor_user_id=uuid.uuid4(),
                )

    async def test_raises_when_already_deleted(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        org_id = await _create_org(db_engine, "already-deleted")
        user_id = await _create_user(db_engine, org_id, "gone@test.com")

        # First deletion request succeeds (must commit so the soft-delete sticks)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            await request_org_deletion(session, org_id, user_id)
            await session.commit()

        # Second request raises
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            with pytest.raises(ValueError, match="already deleted"):
                await request_org_deletion(session, org_id, user_id)

    async def test_soft_deletes_org_and_sets_token(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        org_id = await _create_org(db_engine, "soft-delete")
        user_id = await _create_user(db_engine, org_id, "soft@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        assert "token" in result
        assert len(result["token"]) > 20
        assert "token_expires_at" in result
        assert "export" in result

        state = await _get_org_status(db_engine, org_id)
        assert state["status"] == "deleted"
        assert state["deleted_at"] is not None
        assert state["deletion_token"] == result["token"]
        assert state["deletion_token_expires_at"] is not None
        assert state["export_bundle_json"] is not None

    async def test_soft_delete_retains_child_rows(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        org_id = await _create_org(db_engine, "child-rows")
        user_id = await _create_user(db_engine, org_id, "child@test.com")
        await _create_pipeline(db_engine, org_id, "Child Pipeline", user_id)

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            await request_org_deletion(session, org_id, user_id)
            await session.commit()

        # The org is deactivated (soft-delete)...
        state = await _get_org_status(db_engine, org_id)
        assert state["status"] == "deleted"
        assert state["deleted_at"] is not None

        # ...but child data is retained during the grace window (PRD §org
        # deletion: "Data retained for 30 days"). Child rows are not hard-deleted
        # until confirmation.
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM pipelines WHERE organisation_id = :oid"),
                {"oid": str(org_id)},
            )
            assert result.scalar_one() == 1

    async def test_export_bundle_contains_all_sections(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        org_id = await _create_org(db_engine, "export-test")
        user_id = await _create_user(db_engine, org_id, "export@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        export = result["export"]
        assert "organisation" in export
        assert "memberships" in export
        assert "pipelines" in export
        assert "runs" in export
        assert "audit_events" in export
        assert "library_primitives" in export
        assert "connector_instances" in export
        assert "model_backends" in export
        assert "exported_at" in export


# ── Tests: confirm_org_deletion ─────────────────────────────────────


class TestConfirmOrgDeletion:
    async def test_raises_when_org_not_found(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import confirm_org_deletion

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(ValueError, match="Organisation not found"):
                await confirm_org_deletion(session, org_id=uuid.uuid4(), token="anything")

    async def test_raises_when_token_invalid(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "invalid-token")
        user_id = await _create_user(db_engine, org_id, "invalid@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            await request_org_deletion(session, org_id, user_id)
            await session.flush()

        async with factory() as session:
            with pytest.raises(ValueError, match="Invalid deletion token"):
                await confirm_org_deletion(session, org_id=org_id, token="wrong-token")

    async def test_confirms_with_correct_token(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "confirm-ok")
        user_id = await _create_user(db_engine, org_id, "confirm@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async with factory() as session:
            result = await confirm_org_deletion(session, org_id=org_id, token=result["token"])
            await session.commit()
        assert result["deleted_organisation_id"] == str(org_id)

        # Org should be gone
        async with db_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT COUNT(*) FROM organisations WHERE id = :id"),
                {"id": str(org_id)},
            )
            assert row.scalar_one() == 0

    async def test_immediate_skips_token_check(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "immediate")
        user_id = await _create_user(db_engine, org_id, "immediate@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            await request_org_deletion(session, org_id, user_id)
            await session.flush()

        async with factory() as session:
            result = await confirm_org_deletion(session, org_id=org_id, token="ignored", immediate=True)
            await session.commit()
        assert result["deleted_organisation_id"] == str(org_id)

    async def test_raises_when_token_expired(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import confirm_org_deletion

        org_id = await _create_org(db_engine, "expired-token")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            expired_at = datetime.now(UTC) - timedelta(hours=1)
            await session.execute(
                text(
                    "UPDATE organisations SET status='deleted', "
                    "deletion_token=:token, "
                    "deletion_token_expires_at=:expires "
                    "WHERE id=:id",
                ),
                {
                    "token": "expired-token-value",
                    "expires": expired_at,
                    "id": str(org_id),
                },
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(ValueError, match="has expired"):
                await confirm_org_deletion(session, org_id=org_id, token="expired-token-value")

    async def test_refuses_when_non_terminal_run_exists(self, db_engine: AsyncEngine) -> None:
        """B7 guard: confirm refuses the hard-delete while a live run exists."""
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "live-run")
        user_id = await _create_user(db_engine, org_id, "live-run@test.com")
        pid = await _create_pipeline(db_engine, org_id, "Live Pipeline", user_id)
        sid = await _create_snapshot(db_engine, org_id, pid)
        await _insert_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id="thread-live-run",
            status="running",
        )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async with factory() as session:
            with pytest.raises(ValueError, match="1 run\\(s\\) still in progress"):
                await confirm_org_deletion(session, org_id=org_id, token=result["token"])

        # Org still present — nothing was deleted.
        async with db_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT COUNT(*) FROM organisations WHERE id = :id"),
                {"id": str(org_id)},
            )
            assert row.scalar_one() == 1

    async def test_force_proceeds_with_non_terminal_run(self, db_engine: AsyncEngine) -> None:
        """B7 guard: force=True proceeds (destructive) despite the live run."""
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "force-delete")
        user_id = await _create_user(db_engine, org_id, "force@test.com")
        pid = await _create_pipeline(db_engine, org_id, "Force Pipeline", user_id)
        sid = await _create_snapshot(db_engine, org_id, pid)
        await _insert_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id="thread-force",
            status="awaiting_human",
        )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async with factory() as session:
            outcome = await confirm_org_deletion(
                session,
                org_id=org_id,
                token=result["token"],
                force=True,
            )
            await session.commit()
        assert outcome["deleted_organisation_id"] == str(org_id)

        async with db_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT COUNT(*) FROM organisations WHERE id = :id"),
                {"id": str(org_id)},
            )
            assert row.scalar_one() == 0

    async def test_confirm_invalidates_token_single_use(self, db_engine: AsyncEngine) -> None:
        """A confirmed token is single-use — a second confirm with the same
        token fails (the org row is gone, so the token check raises
        ``Organisation not found`` / the org no longer exists)."""
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "single-use-confirm")
        user_id = await _create_user(db_engine, org_id, "single@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async with factory() as session:
            await confirm_org_deletion(session, org_id=org_id, token=result["token"])
            await session.commit()

        # Replay of the same token must fail — the org row is gone.
        async with factory() as session:
            with pytest.raises(ValueError, match="Organisation not found"):
                await confirm_org_deletion(session, org_id=org_id, token=result["token"])

    async def test_cancel_invalidates_token_single_use(self, db_engine: AsyncEngine) -> None:
        """A cancelled deletion's token is invalidated — the same token can no
        longer confirm, and a fresh request mints a NEW token."""
        from modulo.db.crud.org_deletion import (
            cancel_org_deletion,
            confirm_org_deletion,
            request_org_deletion,
        )

        org_id = await _create_org(db_engine, "single-use-cancel")
        user_id = await _create_user(db_engine, org_id, "cancel@test.com")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async with factory() as session:
            await cancel_org_deletion(session, org_id)
            await session.commit()

        # The cancelled token is cleared on the org row.
        state = await _get_org_status(db_engine, org_id)
        assert state["status"] == "active"
        assert state["deletion_token"] is None

        # Confirm with the old token now fails (single-use enforced by cancel).
        async with factory() as session:
            with pytest.raises(ValueError, match="Invalid deletion token"):
                await confirm_org_deletion(session, org_id=org_id, token=result["token"])

        # A fresh request mints a new token — the org can be deleted properly.
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            fresh = await request_org_deletion(session, org_id, user_id)
            await session.commit()
        assert fresh["token"] != result["token"]

        async with factory() as session:
            outcome = await confirm_org_deletion(session, org_id=org_id, token=fresh["token"])
            await session.commit()
        assert outcome["deleted_organisation_id"] == str(org_id)

    async def test_terminal_runs_batch_deleted_before_fk_cascade(self, db_engine: AsyncEngine) -> None:
        """Old TERMINAL runs are batch-deleted during confirm BEFORE the FK
        cascade, so the cascade only churns a small remaining set (deadlock
        avoidance). A young terminal run survives the batch pass and is removed
        by the cascade — either way the org ends up fully deleted."""
        from modulo.db.crud.org_deletion import confirm_org_deletion, request_org_deletion

        org_id = await _create_org(db_engine, "batch-runs")
        user_id = await _create_user(db_engine, org_id, "batch@test.com")
        pid = await _create_pipeline(db_engine, org_id, "Batch Pipeline", user_id)
        sid = await _create_snapshot(db_engine, org_id, pid)

        old_thread = "thread-old-terminal"
        fresh_thread = "thread-fresh-terminal"
        old_run = await _insert_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id=old_thread,
            status="complete",
        )
        fresh_run = await _insert_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id=fresh_thread,
            status="failed",
        )

        # Age the OLD run past the 30-day retention window so the batch pass
        # targets it; leave the fresh one within the window.
        aged = datetime.now(UTC) - timedelta(days=31)
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("UPDATE runs SET created_at = :aged WHERE id = :rid"),
                {"aged": aged, "rid": str(old_run)},
            )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            result = await request_org_deletion(session, org_id, user_id)
            await session.commit()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            outcome = await confirm_org_deletion(session, org_id=org_id, token=result["token"])
            await session.commit()

        # The batch pass purged the aged terminal run before the cascade.
        assert outcome["hard_deleted_runs"] == 1
        assert outcome["deleted_organisation_id"] == str(org_id)

        # Both runs are gone (batch delete + FK cascade).
        async with db_engine.connect() as conn:
            for rid in (old_run, fresh_run):
                row = await conn.execute(
                    text("SELECT COUNT(*) FROM runs WHERE id = :rid"),
                    {"rid": str(rid)},
                )
                assert row.scalar_one() == 0, f"run {rid} must be gone after org delete"
            row = await conn.execute(
                text("SELECT COUNT(*) FROM organisations WHERE id = :id"),
                {"id": str(org_id)},
            )
            assert row.scalar_one() == 0


# ── Tests: export_org_data ──────────────────────────────────────────


class TestExportOrgData:
    async def test_raises_when_org_not_found(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import export_org_data

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(ValueError, match="Organisation not found"):
                await export_org_data(session, org_id=uuid.uuid4())

    async def test_returns_existing_bundle(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import export_org_data

        org_id = await _create_org(db_engine, "existing-bundle")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("UPDATE organisations SET export_bundle_json = :bundle WHERE id = :id"),
                {
                    "bundle": json.dumps({"organisation": [{"name": "Cached Org"}]}),
                    "id": str(org_id),
                },
            )
            await session.commit()

        async with factory() as session:
            bundle = await export_org_data(session, org_id)
        assert bundle["organisation"][0]["name"] == "Cached Org"

    async def test_collects_live_data_when_no_bundle(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import export_org_data

        org_id = await _create_org(db_engine, "live-export")
        user_id = await _create_user(db_engine, org_id, "live-export@test.com")
        await _create_pipeline(db_engine, org_id, "Live Pipeline", user_id)

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            bundle = await export_org_data(session, org_id)
        assert "organisation" in bundle
        assert "memberships" in bundle
        assert "pipelines" in bundle
        assert bundle["memberships"]
        assert bundle["pipelines"]


# ── Tests: batch_delete_langgraph_checkpoints ───────────────────────


class TestBatchDeleteLanggraphCheckpoints:
    async def test_deletes_only_expired_rows_across_all_saver_tables(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Retention purges expired checkpoint/blobs/writes but keeps fresh rows.

        The checkpoint tables are created unqualified in the test DB by the
        integration conftest (which runs ``ModuloPostgresSaver`` migrations),
        mirroring production. The retention job must delete only rows whose
        ``created_at`` predates the 30-day cutoff AND whose owning run is
        terminal (``thread-old`` maps to a ``complete`` run), across all three
        saver tables. ``thread-fresh``'s run is still ``running``, so its
        rows — even though they are also fresh — are untouched.
        """
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        old_ts = datetime.now(UTC) - timedelta(days=31)
        fresh_ts = datetime.now(UTC)

        # Owning runs for the retention guard: thread-old maps to a TERMINAL
        # run (purged), thread-fresh to a still-running run (kept). The org,
        # user, pipeline, snapshot, and run must all exist before the
        # checkpoint rows are inserted (FKs + tenant triggers).
        old_org = await _create_org(db_engine, "old-owner")
        user = await _create_user(db_engine, old_org, "old-owner@test.com")
        pid = await _create_pipeline(db_engine, old_org, "Pipeline old", user)
        sid = await _create_snapshot(db_engine, old_org, pid)
        await _insert_run(
            db_engine,
            org_id=old_org,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id="thread-old",
            status="complete",
        )

        fresh_org = await _create_org(db_engine, "fresh-owner")
        user = await _create_user(db_engine, fresh_org, "fresh-owner@test.com")
        pid = await _create_pipeline(db_engine, fresh_org, "Pipeline fresh", user)
        sid = await _create_snapshot(db_engine, fresh_org, pid)
        await _insert_run(
            db_engine,
            org_id=fresh_org,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id="thread-fresh",
            status="running",
        )

        async with db_engine.connect() as conn, conn.begin():
            for org, thread, ckp, created_at in (
                (old_org, "thread-old", "ckp-old", old_ts),
                (fresh_org, "thread-fresh", "ckp-fresh", fresh_ts),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, "
                        "checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata, created_at) "
                        "VALUES (:org, :thread, '', :ckp, NULL, NULL, '{}'::jsonb, '{}'::jsonb, :created_at)",
                    ),
                    {"org": str(org), "thread": thread, "ckp": ckp, "created_at": created_at},
                )
                await conn.execute(
                    text(
                        "INSERT INTO checkpoint_blobs (organisation_id, thread_id, checkpoint_ns, "
                        "channel, version, type, blob, created_at) "
                        "VALUES (:org, :thread, '', :channel, :version, 'bytes', :blob, :created_at)",
                    ),
                    {
                        "org": str(org),
                        "thread": thread,
                        "channel": "channel",
                        "version": "v1",
                        "blob": b"blob-data",
                        "created_at": created_at,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO checkpoint_writes (organisation_id, thread_id, checkpoint_ns, "
                        "checkpoint_id, task_id, idx, channel, type, blob, created_at) "
                        "VALUES (:org, :thread, '', :ckp, :task, 0, 'channel', 'json', :blob, :created_at)",
                    ),
                    {
                        "org": str(org),
                        "thread": thread,
                        "ckp": ckp,
                        "task": "task-1",
                        "blob": b"write-data",
                        "created_at": created_at,
                    },
                )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            count = await batch_delete_langgraph_checkpoints(session)
            await session.commit()

        assert count == 3  # one expired row per saver table for the terminal thread

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoints WHERE organisation_id = :oid"),
                {"oid": str(old_org)},
            )
            assert result.scalar_one() == 0
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoint_blobs WHERE organisation_id = :oid"),
                {"oid": str(old_org)},
            )
            assert result.scalar_one() == 0
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoint_writes WHERE organisation_id = :oid"),
                {"oid": str(old_org)},
            )
            assert result.scalar_one() == 0

            # Fresh rows survive the retention pass.
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoints WHERE organisation_id = :oid"),
                {"oid": str(fresh_org)},
            )
            assert result.scalar_one() == 1
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoint_blobs WHERE organisation_id = :oid"),
                {"oid": str(fresh_org)},
            )
            assert result.scalar_one() == 1
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoint_writes WHERE organisation_id = :oid"),
                {"oid": str(fresh_org)},
            )
            assert result.scalar_one() == 1

    async def test_batches_until_all_expired_rows_removed(self, db_engine: AsyncEngine) -> None:
        """The batch loop repeats until fewer than ``batch_size`` rows remain."""
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        old_ts = datetime.now(UTC) - timedelta(days=31)
        org = uuid.uuid4()

        async with db_engine.connect() as conn, conn.begin():
            for i in range(5):
                await conn.execute(
                    text(
                        "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, "
                        "checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata, created_at) "
                        "VALUES (:org, :thread, '', :ckp, NULL, NULL, '{}'::jsonb, '{}'::jsonb, :created_at)",
                    ),
                    {
                        "org": str(org),
                        "thread": f"thread-{i}",
                        "ckp": f"ckp-{i}",
                        "created_at": old_ts,
                    },
                )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            count = await batch_delete_langgraph_checkpoints(session, batch_size=2)
            await session.commit()

        assert count == 5

        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoints WHERE organisation_id = :oid"),
                {"oid": str(org)},
            )
            assert result.scalar_one() == 0

    async def test_never_purges_live_run_checkpoints(self, db_engine: AsyncEngine) -> None:
        """CRITICAL: checkpoints of NON-terminal runs survive the retention pass.

        An ``awaiting_human`` run paused >30 days at a HITL gate must keep its
        interrupt checkpoint — ``resume_run`` reads it on later approval.
        Purging it would make LangGraph restart the graph from scratch,
        re-running side-effectful nodes (duplicate PRs/emails/notifications).
        """
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        org_id = await _create_org(db_engine, "live-ckpt")
        user_id = await _create_user(db_engine, org_id, "live@test.com")
        pid = await _create_pipeline(db_engine, org_id, "Live Pipeline", user_id)
        sid = await _create_snapshot(db_engine, org_id, pid)
        thread = "thread-live"
        await _insert_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id=thread,
            status="awaiting_human",
        )

        old_ts = datetime.now(UTC) - timedelta(days=31)
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, "
                    "checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata, created_at) "
                    "VALUES (:org, :thread, '', :ckp, NULL, NULL, '{}'::jsonb, '{}'::jsonb, :created_at)",
                ),
                {"org": str(org_id), "thread": thread, "ckp": "ckp-live", "created_at": old_ts},
            )
            await conn.execute(
                text(
                    "INSERT INTO checkpoint_blobs (organisation_id, thread_id, checkpoint_ns, "
                    "channel, version, type, blob, created_at) "
                    "VALUES (:org, :thread, '', :channel, :version, 'bytes', :blob, :created_at)",
                ),
                {
                    "org": str(org_id),
                    "thread": thread,
                    "channel": "channel",
                    "version": "v1",
                    "blob": b"blob-data",
                    "created_at": old_ts,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO checkpoint_writes (organisation_id, thread_id, checkpoint_ns, "
                    "checkpoint_id, task_id, idx, channel, type, blob, created_at) "
                    "VALUES (:org, :thread, '', :ckp, :task, 0, 'channel', 'json', :blob, :created_at)",
                ),
                {
                    "org": str(org_id),
                    "thread": thread,
                    "ckp": "ckp-live",
                    "task": "task-1",
                    "blob": b"write-data",
                    "created_at": old_ts,
                },
            )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            count = await batch_delete_langgraph_checkpoints(session)
            await session.commit()

        assert count == 0
        async with db_engine.connect() as conn:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                result = await conn.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE organisation_id = :oid"),  # noqa: S608
                    {"oid": str(org_id)},
                )
                assert result.scalar_one() == 1, f"live-run {table} row must survive retention"

    async def test_fresh_checkpoint_referencing_old_blob_survives(self, db_engine: AsyncEngine) -> None:
        """CRITICAL #2: a fresh checkpoint referencing an old blob keeps the blob.

        An idle channel's blob row can predate the cutoff while its checkpoint
        is fresh. ``_CHECKPOINT_SELECT_SQL`` reconstructs channel_values by
        joining ``checkpoint_blobs`` on the checkpoint's ``channel_versions``;
        deleting the old blob would make the SELECT return NULL channel data
        for the fresh checkpoint. Blobs are only purged when NO checkpoint
        remains in their thread.
        """
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        org_id = await _create_org(db_engine, "idle-ckpt")
        user_id = await _create_user(db_engine, org_id, "idle@test.com")
        pid = await _create_pipeline(db_engine, org_id, "Idle Pipeline", user_id)
        sid = await _create_snapshot(db_engine, org_id, pid)
        thread = "thread-idle"
        await _insert_run(
            db_engine,
            org_id=org_id,
            pipeline_id=pid,
            snapshot_id=sid,
            thread_id=thread,
            status="complete",
        )

        old_ts = datetime.now(UTC) - timedelta(days=31)
        fresh_ts = datetime.now(UTC)
        async with db_engine.connect() as conn, conn.begin():
            for ckp, created_at in (("ckp-old", old_ts), ("ckp-fresh", fresh_ts)):
                await conn.execute(
                    text(
                        "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, "
                        "checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata, created_at) "
                        "VALUES (:org, :thread, '', :ckp, NULL, NULL, '{}'::jsonb, '{}'::jsonb, :created_at)",
                    ),
                    {"org": str(org_id), "thread": thread, "ckp": ckp, "created_at": created_at},
                )
            await conn.execute(
                text(
                    "INSERT INTO checkpoint_blobs (organisation_id, thread_id, checkpoint_ns, "
                    "channel, version, type, blob, created_at) "
                    "VALUES (:org, :thread, '', :channel, :version, 'bytes', :blob, :created_at)",
                ),
                {
                    "org": str(org_id),
                    "thread": thread,
                    "channel": "idle",
                    "version": "v1",
                    "blob": b"blob-data",
                    "created_at": old_ts,
                },
            )

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            count = await batch_delete_langgraph_checkpoints(session)
            await session.commit()

        assert count == 1  # only the OLD checkpoint is purged
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoints WHERE organisation_id = :oid"),
                {"oid": str(org_id)},
            )
            assert result.scalar_one() == 1, "fresh checkpoint must survive"
            result = await conn.execute(
                text("SELECT COUNT(*) FROM checkpoint_blobs WHERE organisation_id = :oid"),
                {"oid": str(org_id)},
            )
            assert result.scalar_one() == 1, "blob referenced by the fresh checkpoint must survive"
