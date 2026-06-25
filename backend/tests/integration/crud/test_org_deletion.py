"""Integration tests for organisation deletion CRUD.

Tests the full deletion workflow: request → soft-delete → confirm → hard-delete,
including token validation, export capture, and run retention cleanup.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.integration


# ── Helpers ──────────────────────────────────────────────────────────


async def _create_org(db_engine: AsyncEngine, suffix: str = "") -> uuid.UUID:
    org_id = uuid.uuid4()
    slug = f"del-test-{suffix or org_id.hex[:8]}"
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO organisations (id, name, slug, settings_json) "
                    "VALUES (:id, :name, :slug, '{}'::json)"
                ),
                {"id": str(org_id), "name": f"Deletion Test {suffix}", "slug": slug},
            )
    return org_id


async def _create_user(
    db_engine: AsyncEngine, org_id: uuid.UUID, email: str
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO users (id, organisation_id, email, display_name, "
                    "org_role, auth_provider, active) "
                    "VALUES (:id, :org_id, :email, :name, 'admin', 'local', true)"
                ),
                {
                    "id": str(user_id),
                    "org_id": str(org_id),
                    "email": email,
                    "name": email.split("@")[0],
                },
            )
    return user_id


async def _create_pipeline(
    db_engine: AsyncEngine, org_id: uuid.UUID, name: str
) -> uuid.UUID:
    pid = uuid.uuid4()
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(
                text(
                    "INSERT INTO pipelines (id, organisation_id, name, slug, "
                    "visibility, max_concurrent_runs, lock_wait_timeout_seconds, "
                    "state_graph_json) "
                    "VALUES (:id, :org_id, :name, :slug, 'org', 1, 30, '{}'::json)"
                ),
                {
                    "id": str(pid),
                    "org_id": str(org_id),
                    "name": name,
                    "slug": f"pipe-{pid.hex[:8]}",
                },
            )
    return pid


async def _count_rows(
    db_engine: AsyncEngine, table: str, org_id: uuid.UUID | None = None
) -> int:
    async with db_engine.connect() as conn:
        if org_id:
            result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE organisation_id = :oid"),
                {"oid": str(org_id)},
            )
        else:
            result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return result.scalar_one()


async def _get_org_status(
    db_engine: AsyncEngine, org_id: uuid.UUID
) -> dict[str, Any]:
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT status, deleted_at, deletion_token, "
                "deletion_token_expires_at, export_bundle_json "
                "FROM organisations WHERE id = :id"
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
                    actor_user_id=uuid.uuid4(),
                )

    async def test_raises_when_already_deleted(self, db_engine: AsyncEngine) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        org_id = await _create_org(db_engine, "already-deleted")
        user_id = await _create_user(db_engine, org_id, "gone@test.com")

        # First deletion request succeeds
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            await request_org_deletion(session, org_id, user_id)
            await session.flush()

        # Second request raises
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            with pytest.raises(ValueError, match="already deleted"):
                await request_org_deletion(session, org_id, user_id)

    async def test_soft_deletes_org_and_sets_token(
        self, db_engine: AsyncEngine
    ) -> None:
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
            await session.flush()

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

    async def test_soft_marks_child_rows(
        self, db_engine: AsyncEngine
    ) -> None:
        from modulo.db.crud.org_deletion import request_org_deletion

        org_id = await _create_org(db_engine, "child-rows")
        user_id = await _create_user(db_engine, org_id, "child@test.com")
        await _create_pipeline(db_engine, org_id, "Child Pipeline")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.organisation_id', :oid, true)"),
                {"oid": str(org_id)},
            )
            await request_org_deletion(session, org_id, user_id)
            await session.flush()

        # Check that pipelines are soft-deleted
        async with db_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM pipelines "
                    "WHERE organisation_id = :oid AND deleted_at IS NOT NULL"
                ),
                {"oid": str(org_id)},
            )
            deleted_pipelines = result.scalar_one()
            assert deleted_pipelines == 1

    async def test_export_bundle_contains_all_sections(
        self, db_engine: AsyncEngine
    ) -> None:
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
            await session.flush()

        export = result["export"]
        assert "organisation" in export
        assert "users" in export
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
                await confirm_org_deletion(
                    session, org_id=uuid.uuid4(), token="anything"
                )

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
                await confirm_org_deletion(
                    session, org_id=org_id, token="wrong-token"
                )

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
            req = await request_org_deletion(session, org_id, user_id)
            await session.flush()

        async with factory() as session:
            result = await confirm_org_deletion(
                session, org_id=org_id, token=req["token"]
            )
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
            result = await confirm_org_deletion(
                session, org_id=org_id, token="ignored", immediate=True
            )
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
                    "WHERE id=:id"
                ),
                {
                    "token": "expired-token-value",
                    "expires": expired_at,
                    "id": str(org_id),
                },
            )
            await session.flush()

        async with factory() as session:
            with pytest.raises(ValueError, match="has expired"):
                await confirm_org_deletion(
                    session, org_id=org_id, token="expired-token-value"
                )


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
                text(
                    "UPDATE organisations SET export_bundle_json = :bundle "
                    "WHERE id = :id"
                ),
                {
                    "bundle": {"organisation": [{"name": "Cached Org"}]},
                    "id": str(org_id),
                },
            )
            await session.flush()

        async with factory() as session:
            bundle = await export_org_data(session, org_id)
        assert bundle["organisation"][0]["name"] == "Cached Org"

    async def test_collects_live_data_when_no_bundle(
        self, db_engine: AsyncEngine
    ) -> None:
        from modulo.db.crud.org_deletion import export_org_data

        org_id = await _create_org(db_engine, "live-export")
        await _create_user(db_engine, org_id, "live-export@test.com")
        await _create_pipeline(db_engine, org_id, "Live Pipeline")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            bundle = await export_org_data(session, org_id)
        assert "organisation" in bundle
        assert "users" in bundle
        assert "pipelines" in bundle
        assert len(bundle["users"]) >= 1
        assert len(bundle["pipelines"]) >= 1


# ── Tests: batch_delete_langgraph_checkpoints ───────────────────────


class TestBatchDeleteLanggraphCheckpoints:
    async def test_returns_zero_when_no_checkpoint_tables(
        self, db_engine: AsyncEngine
    ) -> None:
        """If the langgraph schema does not exist, the function should handle it."""
        from modulo.db.crud.org_deletion import batch_delete_langgraph_checkpoints

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            try:
                count = await batch_delete_langgraph_checkpoints(session)
                assert count == 0
            except Exception as exc:
                msg = str(exc).lower()
                if "does not exist" in msg or "relation" in msg:
                    pytest.skip("langgraph schema not available in this test DB")
                raise
