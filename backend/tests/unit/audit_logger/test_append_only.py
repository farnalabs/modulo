"""Unit tests for the append-only audit event guard.

Verifies that SQLAlchemy ORM event listeners correctly block
UPDATE and DELETE operations on AuditEvent records.

Note: Full ORM-level update/delete verification requires a working database.
That's covered in tests/integration/test_audit_append_only.py which uses
real Postgres via Testcontainers.

Here we verify registration, non-blocking inserts, and guard logic.
"""

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.audit_logger.append_only import (
    AppendOnlyViolationError,
    register_append_only_guard,
)
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.error_event import ErrorEvent

_ListenerMap = dict[tuple[type, str], Callable[..., None]]


def _capture_listeners() -> _ListenerMap:
    """Register the guard while intercepting ``event.listen`` so the exact
    listener closures can be fired directly (no DB required)."""
    captured: _ListenerMap = {}

    def _capture(target: type, identifier: str, listener: Callable[..., None]) -> None:
        captured[(target, identifier)] = listener

    with (
        patch("modulo.core.audit_logger.append_only._guard_registered", False),
        patch("modulo.core.audit_logger.append_only.event.listen", side_effect=_capture),
    ):
        register_append_only_guard()
    return captured


def _capture_guard_listeners(monkeypatch) -> dict[tuple, object]:
    """Re-register the guard while recording the exact listener closures.

    Returns ``{(model_class, mutation): listener_fn}`` for every listener that
    ``register_append_only_guard()`` wires up, so tests can assert the real
    registered closures are present and block mutations.
    """
    from modulo.core.audit_logger import append_only

    captured: dict[tuple, object] = {}
    real_make_blocker = append_only._make_blocker

    def _recording_blocker(model_class, table_name, mutation):
        fn = real_make_blocker(model_class, table_name, mutation)
        captured[(model_class, mutation)] = fn
        return fn

    monkeypatch.setattr(append_only, "_make_blocker", _recording_blocker)
    monkeypatch.setattr(append_only, "_guard_registered", False)
    register_append_only_guard()
    return captured


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

    def test_listeners_are_registered(self, monkeypatch):
        """Verify event listeners are actually registered on both append-only models."""
        from sqlalchemy import event as sa_event

        from modulo.core.audit_logger import append_only

        captured = _capture_guard_listeners(monkeypatch)
        expected = {
            (AuditEvent, "update"),
            (AuditEvent, "delete"),
            (ErrorEvent, "update"),
            (ErrorEvent, "delete"),
        }

        assert set(captured) == expected
        assert append_only._guard_registered is True
        for model, mutation in expected:
            identifier = f"before_{mutation}"
            assert sa_event.contains(model, identifier, captured[(model, mutation)])


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


class TestAppendOnlyGuardListenerLogic:
    """Tests the guard's listener logic directly by firing the registered
    listeners (no database required)."""

    def test_update_listener_blocks(self):
        """The registered before_update listener raises AppendOnlyViolationError."""
        from modulo.core.audit_logger.append_only import (
            AppendOnlyViolationError,
            _make_blocker,
        )

        blocker = _make_blocker(AuditEvent, "audit_events", "update")
        event = AuditEvent(organisation_id=uuid.uuid4(), event_type="test")
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        assert "append-only" in str(exc_info.value).lower()
        assert str(event.id) in str(exc_info.value)

    def test_delete_listener_blocks(self):
        """The registered before_delete listener raises AppendOnlyViolationError."""
        from modulo.core.audit_logger.append_only import (
            AppendOnlyViolationError,
            _make_blocker,
        )

        blocker = _make_blocker(AuditEvent, "audit_events", "delete")
        event = AuditEvent(organisation_id=uuid.uuid4(), event_type="test")
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        assert "append-only" in str(exc_info.value).lower()
        assert str(event.id) in str(exc_info.value)

    def test_update_listener_rejects_audit_event(self):
        """The before_update listener must raise AppendOnlyViolationError."""
        listeners = _capture_listeners()
        blocker = listeners[(AuditEvent, "before_update")]

        event = AuditEvent(
            organisation_id=uuid.uuid4(),
            event_type="test.event",
            payload_json={"key": "value"},
        )
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        assert str(event.id) in str(exc_info.value)
        assert "cannot be updated" in str(exc_info.value)
        assert "append-only" in str(exc_info.value)

    def test_registered_update_listener_blocks(self, monkeypatch):
        """The listener actually registered on AuditEvent blocks an update."""
        from sqlalchemy import event as sa_event

        from modulo.core.audit_logger.append_only import AppendOnlyViolationError

        captured = _capture_guard_listeners(monkeypatch)

        event = AuditEvent(
            organisation_id=uuid.uuid4(),
            event_type="test.event",
            payload_json={"key": "value"},
        )
        event.id = uuid.uuid4()

        registered_fn = captured[(AuditEvent, "update")]
        assert sa_event.contains(AuditEvent, "before_update", registered_fn)
        with pytest.raises(AppendOnlyViolationError, match="append-only"):
            registered_fn(None, None, event)

    def test_delete_listener_rejects_error_event(self):
        """The before_delete listener must raise AppendOnlyViolationError."""
        listeners = _capture_listeners()
        blocker = listeners[(ErrorEvent, "before_delete")]

        event = ErrorEvent(
            organisation_id=uuid.uuid4(),
            fingerprint="fp-1",
            level="error",
            message="boom",
            source="backend",
        )
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        assert str(event.id) in str(exc_info.value)
        assert "cannot be deleted" in str(exc_info.value)
        assert "append-only" in str(exc_info.value)

    def test_registered_delete_listener_blocks(self, monkeypatch):
        """The listener actually registered on AuditEvent blocks a delete."""
        from sqlalchemy import event as sa_event

        from modulo.core.audit_logger.append_only import AppendOnlyViolationError

        captured = _capture_guard_listeners(monkeypatch)

        event = ErrorEvent(
            organisation_id=uuid.uuid4(),
            fingerprint="fp-1",
            level="error",
            message="boom",
            source="backend",
        )
        event.id = uuid.uuid4()

        registered_fn = captured[(AuditEvent, "delete")]
        assert sa_event.contains(AuditEvent, "before_delete", registered_fn)
        with pytest.raises(AppendOnlyViolationError, match="append-only"):
            registered_fn(None, None, event)

    def test_update_listener_rejects_error_event(self):
        """ErrorEvent must also be protected against UPDATEs."""
        listeners = _capture_listeners()
        blocker = listeners[(ErrorEvent, "before_update")]

        event = ErrorEvent(
            organisation_id=uuid.uuid4(),
            fingerprint="fp-2",
            level="warning",
            message="warn",
            source="frontend",
        )
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        assert str(event.id) in str(exc_info.value)
        assert "cannot be updated" in str(exc_info.value)

    def test_error_event_is_guarded(self, monkeypatch):
        """ErrorEvent is also protected by the append-only guard."""
        from sqlalchemy import event as sa_event

        from modulo.core.audit_logger.append_only import AppendOnlyViolationError
        from modulo.db.models.error_event import ErrorEvent

        captured = _capture_guard_listeners(monkeypatch)

        event = ErrorEvent(
            organisation_id=uuid.uuid4(),
            fingerprint="f" * 64,
            level="error",
            message="boom",
            source="backend",
        )
        event.id = uuid.uuid4()

        registered_fn = captured[(ErrorEvent, "update")]
        assert sa_event.contains(ErrorEvent, "before_update", registered_fn)
        with pytest.raises(AppendOnlyViolationError, match="append-only"):
            registered_fn(None, None, event)

    def test_listener_uses_target_id_in_message(self):
        """The error message must identify the specific record being mutated."""
        listeners = _capture_listeners()
        blocker = listeners[(AuditEvent, "before_update")]

        event = AuditEvent(organisation_id=uuid.uuid4(), event_type="test.event")
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        assert str(event.id) in str(exc_info.value)


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
