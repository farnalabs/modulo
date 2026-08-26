"""Application-layer append-only guard for AuditEvent and ErrorEvent records.

Provides defense-in-depth via SQLAlchemy ORM event listeners that prevent
any UPDATE or DELETE operations on append-only table instances at the ORM
level. This complements the database-level Postgres triggers.

Usage:
    from modulo.core.audit_logger.append_only import register_append_only_guard
    register_append_only_guard()
"""

import logging
import threading
from collections.abc import Callable

from sqlalchemy import event

from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.error_event import ErrorEvent

_log = logging.getLogger(__name__)

_guard_lock = threading.Lock()
_guard_registered = False


class AppendOnlyViolationError(RuntimeError):
    """Raised when an UPDATE or DELETE is attempted on an append-only table."""


def _make_blocker(model_class: type, table_name: str, mutation: str) -> Callable[..., None]:
    """Create an ORM event listener that blocks UPDATE or DELETE."""

    def _block(
        _mapper: object,
        _: object,
        target: object,
    ) -> None:
        eid = getattr(target, "id", "?")
        raise AppendOnlyViolationError(
            f"{model_class.__name__} {eid} cannot be {mutation}d: {table_name} are append-only"
        )

    return _block


def _register_blocker(model_class: type, table_name: str) -> None:
    """Register before_update and before_delete listeners for a model."""
    event.listen(model_class, "before_update", _make_blocker(model_class, table_name, "update"))
    event.listen(model_class, "before_delete", _make_blocker(model_class, table_name, "delete"))
    _log.info("Registered append-only guard on %s (UPDATE/DELETE blocked)", model_class.__name__)


def register_append_only_guard() -> None:
    """Register ORM event listeners that block UPDATE/DELETE on append-only models.

    Safe to call multiple times — only the first call registers listeners.
    Thread-safe via module-level lock.
    """
    global _guard_registered
    with _guard_lock:
        if _guard_registered:
            return
        _register_blocker(AuditEvent, AuditEvent.__tablename__)
        _register_blocker(ErrorEvent, ErrorEvent.__tablename__)
        _guard_registered = True
