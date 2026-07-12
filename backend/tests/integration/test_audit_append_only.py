"""Integration tests for the append-only audit event invariant.

Verifies that both the Postgres-level trigger and the ORM-level
event listeners prevent UPDATE/DELETE on audit_events.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.models.audit_event import AuditEvent


class TestAuditAppendOnlyDbTrigger:
    """Tests that the Postgres trigger prevents UPDATE/DELETE at the DB level."""

    @pytest_asyncio.fixture(autouse=True)
    async def _seed_event(self, db_session: AsyncSession) -> None:
        """Insert a test audit event for use by test methods via db_session.info."""
        org_id = uuid.uuid4()

        await db_session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": "Audit Test Org", "slug": f"audit-{org_id.hex[:8]}"},
        )
        await db_session.commit()

        event_id = uuid.uuid4()

        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {
                "id": event_id,
                "org_id": org_id,
                "event_type": "test.event",
            },
        )
        await db_session.commit()

    async def test_update_trigger_blocks_update(self, db_session: AsyncSession) -> None:
        """UPDATE on audit_events should be rejected by the Postgres trigger."""
        event_id = await db_session.execute(
            text("SELECT id FROM audit_events WHERE event_type = 'test.event' LIMIT 1"),
        )
        event_id = event_id.scalar_one()
        with pytest.raises(DBAPIError) as exc_info:
            await db_session.execute(
                text("UPDATE audit_events SET event_type = 'modified' WHERE id = :id"),
                {"id": event_id},
            )
        error_msg = str(exc_info.value).lower()
        assert "append-only" in error_msg or "not permitted" in error_msg

    async def test_delete_trigger_blocks_delete(self, db_session: AsyncSession) -> None:
        """DELETE on audit_events should be rejected by the Postgres trigger."""
        event_id = await db_session.execute(
            text("SELECT id FROM audit_events WHERE event_type = 'test.event' LIMIT 1"),
        )
        event_id = event_id.scalar_one()
        with pytest.raises(DBAPIError) as exc_info:
            await db_session.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": event_id},
            )
        error_msg = str(exc_info.value).lower()
        assert "append-only" in error_msg or "not permitted" in error_msg

    async def test_event_still_exists_after_attempted_delete(self, db_session: AsyncSession) -> None:
        """After a failed DELETE attempt, the event should still exist."""
        event_id = await db_session.execute(
            text("SELECT id FROM audit_events WHERE event_type = 'test.event' LIMIT 1"),
        )
        event_id = event_id.scalar_one()

        # Try to delete
        with pytest.raises(DBAPIError):
            await db_session.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": event_id},
            )

        # Roll back the aborted transaction before continuing
        await db_session.rollback()

        # Verify event still exists
        result = await db_session.execute(
            text("SELECT id FROM audit_events WHERE id = :id"),
            {"id": event_id},
        )
        assert result.scalar_one_or_none() is not None


class TestAuditAppendOnlyOrmGuard:
    """Tests that the ORM-level event listeners block UPDATE/DELETE."""

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self) -> None:
        """Register the ORM guard before each test."""
        register_append_only_guard()

    @pytest_asyncio.fixture
    async def fresh_session(self, db_engine: AsyncEngine) -> AsyncSession:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def test_orm_update_blocked(self, fresh_session: AsyncSession) -> None:
        """ORM update should raise RuntimeError for AuditEvent."""
        # Create and persist an event
        org_id = uuid.uuid4()

        await fresh_session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": "ORM Update Org", "slug": f"orm-upd-{org_id.hex[:8]}"},
        )
        await fresh_session.commit()

        # Insert via raw SQL to avoid ORM listeners during creation
        await fresh_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {"id": uuid.uuid4(), "org_id": org_id, "event_type": "orm.test"},
        )
        await fresh_session.commit()

        # Now load via ORM and try to update
        result = await fresh_session.execute(
            text("SELECT id FROM audit_events WHERE organisation_id = :org_id"),
            {"org_id": org_id},
        )
        event_id = result.scalar_one()

        event = await fresh_session.get(AuditEvent, event_id)
        assert event is not None

        event.event_type = "modified"
        with pytest.raises(RuntimeError) as exc_info:
            await fresh_session.flush()
        assert "append-only" in str(exc_info.value).lower()

    async def test_orm_delete_blocked(self, fresh_session: AsyncSession) -> None:
        """ORM delete should raise RuntimeError for AuditEvent."""
        org_id = uuid.uuid4()
        event_id = uuid.uuid4()

        await fresh_session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": "ORM Delete Org", "slug": f"orm-del-{org_id.hex[:8]}"},
        )
        await fresh_session.commit()

        # Insert via raw SQL
        await fresh_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {"id": event_id, "org_id": org_id, "event_type": "orm.test.delete"},
        )
        await fresh_session.commit()

        event = await fresh_session.get(AuditEvent, event_id)
        assert event is not None

        await fresh_session.delete(event)
        with pytest.raises(RuntimeError) as exc_info:
            await fresh_session.flush()
        assert "append-only" in str(exc_info.value).lower()


class TestAuditReadOperations:
    """Verifies that read operations are NOT blocked."""

    async def test_select_still_works(self, db_session: AsyncSession) -> None:
        """SELECT on audit_events should still work."""
        result = await db_session.execute(text("SELECT COUNT(*) FROM audit_events"))
        count = result.scalar()
        assert count is not None

    async def test_insert_still_works(self, db_session: AsyncSession) -> None:
        """INSERT on audit_events should still work."""
        org_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, :name, :slug, '{}'::json, '{}'::json)"
            ),
            {"id": str(org_id), "name": "Insert Test Org", "slug": f"ins-{org_id.hex[:8]}"},
        )
        await db_session.commit()
        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {
                "id": uuid.uuid4(),
                "org_id": org_id,
                "event_type": "test.insert",
            },
        )
        await db_session.commit()
