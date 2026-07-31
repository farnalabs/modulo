"""Unit tests for the append-only audit event guard.

Verifies that SQLAlchemy ORM event listeners correctly block
UPDATE and DELETE operations on AuditEvent records.

Note: Full ORM-level update/delete verification requires a working database.
That's covered in tests/integration/test_audit_append_only.py which uses
real Postgres via Testcontainers.

Here we verify registration, non-blocking inserts, and guard logic.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.audit_logger.append_only import register_append_only_guard
from modulo.db.models.audit_event import AuditEvent


class TestAppendOnlyGuardRegistration:
    """Tests that the guard registers without errors."""

    def test_register_idempotent(self):
        """Calling register multiple times should not error."""
        register_append_only_guard()
        register_append_only_guard()

    def test_update_blocked_by_orm_listener(self):
        """UPDATE on an AuditEvent instance raises AppendOnlyViolationError."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from modulo.core.audit_logger.append_only import (
            AppendOnlyViolationError,
            register_append_only_guard,
        )
        from modulo.db.models.audit_event import AuditEvent
        from modulo.db.models.base import Base

        register_append_only_guard()

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[AuditEvent.__table__])
        with Session(engine) as session:
            event = AuditEvent(organisation_id=uuid.uuid4(), event_type="test.event")
            session.add(event)
            session.commit()

            event.event_type = "mutated"
            with pytest.raises(AppendOnlyViolationError, match="append-only"):
                session.commit()

    def test_delete_blocked_by_orm_listener(self):
        """DELETE on an AuditEvent instance raises AppendOnlyViolationError."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from modulo.core.audit_logger.append_only import (
            AppendOnlyViolationError,
            register_append_only_guard,
        )
        from modulo.db.models.audit_event import AuditEvent
        from modulo.db.models.base import Base

        register_append_only_guard()

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[AuditEvent.__table__])
        with Session(engine) as session:
            event = AuditEvent(organisation_id=uuid.uuid4(), event_type="test.event")
            session.add(event)
            session.commit()

            session.delete(event)
            with pytest.raises(AppendOnlyViolationError, match="append-only"):
                session.commit()


class TestAppendOnlyGuardDoesNotBlockInsert:
    """Tests that INSERT operations (object creation) are NOT blocked."""

    def test_insert_not_blocked(self):
        """Creating AuditEvent instances should not raise."""
        register_append_only_guard()

        event = AuditEvent(
            organisation_id=uuid.uuid4(),
            event_type="test.event",
            payload_json={"key": "value"},
        )
        assert event.event_type == "test.event"
        assert event.payload_json == {"key": "value"}

    def test_multiple_creations_work(self):
        """Multiple AuditEvent objects can be created without error."""
        register_append_only_guard()

        for i in range(10):
            event = AuditEvent(
                organisation_id=uuid.uuid4(),
                event_type=f"test.event.{i}",
                payload_json={"seq": i},
            )
            assert event.event_type == f"test.event.{i}"
            assert event.payload_json == {"seq": i}


class TestAppendOnlyGuardWithMockSession:
    """Tests that append_audit_event still works with guard registered."""

    @pytest.mark.asyncio
    async def test_append_event_still_works(self):
        """Appending new events works without triggering the guard."""
        register_append_only_guard()

        from modulo.core.audit_logger import append_audit_event

        mock_session = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.begin = MagicMock()

        org_id = uuid.uuid4()

        with patch(
            "modulo.core.audit_logger._get_chain_head_locked",
            return_value=None,
        ):
            event = await append_audit_event(
                mock_session,
                org_id=org_id,
                event_type="test.event",
            )

        assert event is not None
        assert event.event_type == "test.event"
        assert event.organisation_id == org_id

    @pytest.mark.asyncio
    async def test_guard_not_triggered_by_append(self):
        """Append-only guard should NOT fire during event creation (INSERT)."""
        register_append_only_guard()

        from modulo.core.audit_logger import append_audit_event

        mock_session = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.begin = MagicMock()

        org_id = uuid.uuid4()

        with patch(
            "modulo.core.audit_logger._get_chain_head_locked",
            return_value=None,
        ):
            # This should complete without RuntimeError
            event = await append_audit_event(
                mock_session,
                org_id=org_id,
                event_type="test.event",
            )
            assert event.organisation_id == org_id
