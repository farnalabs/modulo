"""Integration tests for the append-only audit event invariant.

Verifies that both the Postgres-level trigger and the ORM-level
event listeners prevent UPDATE/DELETE on audit_events.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.models.audit_event import AuditEvent


class TestAuditAppendOnlyDbTrigger:
    """Tests that the Postgres trigger prevents UPDATE/DELETE at the DB level."""

    @pytest_asyncio.fixture(autouse=True)
    async def seed_event(self, db_session: AsyncSession) -> AuditEvent:
        """Insert a test audit event directly via SQL to bypass ORM listeners."""
        org_id = uuid.uuid4()
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
        # Store for test methods
        self._event_id = event_id
        self._org_id = org_id
        return event_id

    async def test_update_trigger_blocks_update(self, db_session: AsyncSession) -> None:
        """UPDATE on audit_events should be rejected by the Postgres trigger."""
        with pytest.raises(Exception) as exc_info:
            await db_session.execute(
                text("UPDATE audit_events SET event_type = 'modified' WHERE id = :id"),
                {"id": self._event_id},
            )
            await db_session.commit()
        error_msg = str(exc_info.value).lower()
        assert "append-only" in error_msg or "not permitted" in error_msg

    async def test_delete_trigger_blocks_delete(self, db_session: AsyncSession) -> None:
        """DELETE on audit_events should be rejected by the Postgres trigger."""
        with pytest.raises(Exception) as exc_info:
            await db_session.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": self._event_id},
            )
            await db_session.commit()
        error_msg = str(exc_info.value).lower()
        assert "append-only" in error_msg or "not permitted" in error_msg

    async def test_event_still_exists_after_attempted_delete(self, db_session: AsyncSession) -> None:
        """After a failed DELETE attempt, the event should still exist."""
        # Try to delete
        with pytest.raises(Exception):
            await db_session.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": self._event_id},
            )
            await db_session.commit()

        # Verify event still exists
        result = await db_session.execute(
            text("SELECT id FROM audit_events WHERE id = :id"),
            {"id": self._event_id},
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
        await db_session.execute(
            text("""
                INSERT INTO audit_events
                    (id, organisation_id, event_type, payload_json, created_at, updated_at)
                VALUES (:id, :org_id, :event_type, '{}'::json, NOW(), NOW())
            """),
            {
                "id": uuid.uuid4(),
                "org_id": uuid.uuid4(),
                "event_type": "test.insert",
            },
        )
        await db_session.commit()
