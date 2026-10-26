"""Unit tests for the append-only audit event guard.

Verifies that SQLAlchemy ORM event listeners correctly block UPDATE and
DELETE operations on AuditEvent and ErrorEvent records.

The guard is exercised at two levels: through a real SQLite session (so
SQLAlchemy actually dispatches ``before_update`` / ``before_delete``), and by
firing the exact registered listener closures directly. The Postgres trigger
half of the invariant is covered by tests/integration/test_audit_append_only.py.
"""

import uuid
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from modulo.core.audit_logger import append_audit_event, append_only
from modulo.core.audit_logger.append_only import (
    AppendOnlyViolationError,
    register_append_only_guard,
)
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.base import Base
from modulo.db.models.error_event import ErrorEvent

_ListenerMap = dict[tuple[type, str], Callable[..., None]]

# ``(model, mutation)`` matrix: every row of the guard's 2 models x 2 mutations.
_MUTATION_MATRIX = [
    (AuditEvent, "update"),
    (AuditEvent, "delete"),
    (ErrorEvent, "update"),
    (ErrorEvent, "delete"),
]
_MUTATION_IDS = [f"{model.__name__.lower()}-{mutation}" for model, mutation in _MUTATION_MATRIX]

_PAST_TENSE = {"update": "updated", "delete": "deleted"}


def _event_for(model: type) -> AuditEvent | ErrorEvent:
    """Build an unsaved instance of an append-only model."""
    if model is AuditEvent:
        return AuditEvent(
            organisation_id=uuid.uuid4(),
            event_type="test.event",
            payload_json={"key": "value"},
        )
    return ErrorEvent(
        organisation_id=uuid.uuid4(),
        fingerprint="f" * 64,
        level="error",
        message="boom",
        source="backend",
    )


def _capture_listeners() -> _ListenerMap:
    """Capture the guard's listener closures without registering them globally.

    ``event.listen`` is intercepted, so the closures can be fired directly
    (no DB required) and repeated captures never accumulate duplicate listeners
    on the model classes.
    """
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


@pytest.fixture
def orm_session() -> Iterator[Session]:
    """A real SQLAlchemy session so the ORM actually fires the guard listeners."""
    register_append_only_guard()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AuditEvent.__table__])
    with Session(engine) as session:
        yield session
    engine.dispose()


class TestAppendOnlyGuardRegistration:
    """Tests that the guard registers without errors."""

    def test_register_idempotent(self):
        """Calling register multiple times should not error."""
        register_append_only_guard()
        register_append_only_guard()
        assert append_only._guard_registered is True

    def test_second_register_does_not_duplicate_listeners(self, monkeypatch):
        """Repeated registration must not re-register ORM listeners (duplicates
        would make the blocker fire multiple times and double-raise)."""
        listen_calls: list[tuple] = []

        def _recording_listen(target, identifier, listener):
            listen_calls.append((target, identifier, listener))

        monkeypatch.setattr(append_only, "_guard_registered", False)
        monkeypatch.setattr(append_only.event, "listen", _recording_listen)

        register_append_only_guard()
        register_append_only_guard()
        register_append_only_guard()

        # 2 models x (before_update + before_delete) = 4 listeners, once.
        assert len(listen_calls) == 4
        assert append_only._guard_registered is True

    def test_listeners_are_registered(self, monkeypatch):
        """Verify event listeners are actually registered on both append-only models."""
        captured = _capture_guard_listeners(monkeypatch)
        expected = set(_MUTATION_MATRIX)

        assert set(captured) == expected
        assert append_only._guard_registered is True
        for (model, mutation), listener in captured.items():
            assert sa_event.contains(model, f"before_{mutation}", listener)


class TestAppendOnlyGuardDoesNotBlockInsert:
    """Tests that INSERT operations (object creation) are NOT blocked."""

    def test_insert_commits_with_guard_registered(self, orm_session: Session):
        """New records persist through the guarded session, repeatedly."""
        for seq in range(3):
            orm_session.add(
                AuditEvent(
                    organisation_id=uuid.uuid4(),
                    event_type=f"test.event.{seq}",
                    payload_json={"seq": seq},
                )
            )
            orm_session.commit()

        rows = orm_session.execute(select(AuditEvent)).scalars().all()
        assert {row.event_type for row in rows} == {f"test.event.{seq}" for seq in range(3)}
        assert sorted(row.payload_json["seq"] for row in rows) == [0, 1, 2]


class TestAppendOnlyGuardOrmDispatch:
    """ORM-level verification: a real session commit is blocked by the guard."""

    @pytest.mark.parametrize("mutation", ["update", "delete"])
    def test_orm_mutation_is_blocked(self, orm_session: Session, mutation: str):
        """UPDATE and DELETE of a persisted AuditEvent raise on commit.

        Unlike firing the closure directly, this goes through SQLAlchemy's
        event dispatch, proving the listener is attached to the mapped class.
        """
        event = AuditEvent(organisation_id=uuid.uuid4(), event_type="test.event")
        orm_session.add(event)
        orm_session.commit()

        if mutation == "update":
            event.event_type = "mutated"
        else:
            orm_session.delete(event)

        with pytest.raises(AppendOnlyViolationError, match="append-only"):
            orm_session.commit()
        orm_session.rollback()


class TestAppendOnlyGuardListenerLogic:
    """Fires the exact listener closures the guard builds (no database required)."""

    @pytest.mark.parametrize(("model", "mutation"), _MUTATION_MATRIX, ids=_MUTATION_IDS)
    def test_listener_blocks_mutation(self, model: type, mutation: str):
        """Each model/mutation listener raises with the record id and reason."""
        listeners = _capture_listeners()
        blocker = listeners[(model, f"before_{mutation}")]
        event = _event_for(model)
        event.id = uuid.uuid4()

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, event)

        message = str(exc_info.value)
        assert str(event.id) in message
        assert f"cannot be {_PAST_TENSE[mutation]}" in message
        assert "append-only" in message

    def test_listener_uses_placeholder_when_target_has_no_id(self):
        """The error message falls back to '?' when the target exposes no id."""
        listeners = _capture_listeners()
        blocker = listeners[(AuditEvent, "before_update")]

        with pytest.raises(AppendOnlyViolationError) as exc_info:
            blocker(None, None, object())

        assert "AuditEvent ?" in str(exc_info.value)
        assert "cannot be updated" in str(exc_info.value)


class TestAppendOnlyGuardWithMockSession:
    """Tests that append_audit_event still works with guard registered."""

    @staticmethod
    async def _append_test_event(mock_session, org_id):
        """Append one event with the chain head stubbed to None (fresh chain)."""
        with patch(
            "modulo.core.audit_logger._get_chain_head_locked",
            return_value=None,
        ):
            return await append_audit_event(
                mock_session,
                org_id=org_id,
                event_type="test.event",
            )

    @staticmethod
    def _mock_session():
        mock_session = MagicMock()
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.execute = AsyncMock()
        mock_session.begin = MagicMock()
        return mock_session

    async def test_append_event_succeeds_with_guard_registered(self):
        """Appending a new event is an INSERT, so the guard does not fire."""
        mock_session = self._mock_session()
        org_id = uuid.uuid4()

        event = await self._append_test_event(mock_session, org_id)

        assert event is not None
        assert event.event_type == "test.event"
        assert event.organisation_id == org_id
