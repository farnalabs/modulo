"""Application-layer append-only guard for AuditEvent and ErrorEvent records.

Provides defense-in-depth via SQLAlchemy ORM event listeners that prevent
any UPDATE or DELETE operations on append-only table instances at the ORM
level. This complements the database-level Postgres triggers.

Usage:
    from modulo.core.audit_logger.append_only import register_append_only_guard
    register_append_only_guard()
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm.mapper import Mapper

from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.error_event import ErrorEvent

_log = logging.getLogger(__name__)

_guard_registered = False


def _register_blocker(model_class: type, table_name: str) -> None:
    """Register before_update and before_delete listeners for a model."""

    def _block_update(
        mapper: Mapper,
        connection: object,
        target: object,
    ) -> None:
        eid = getattr(target, "id", "?")
        raise RuntimeError(
            f"{model_class.__name__} {eid} cannot be updated: {table_name} are append-only"
        )

    def _block_delete(
        mapper: Mapper,
        connection: object,
        target: object,
    ) -> None:
        eid = getattr(target, "id", "?")
        raise RuntimeError(
            f"{model_class.__name__} {eid} cannot be deleted: {table_name} are append-only"
        )

    event.listen(model_class, "before_update", _block_update)
    event.listen(model_class, "before_delete", _block_delete)
    _log.info("Registered append-only guard on %s (UPDATE/DELETE blocked)", model_class.__name__)


def register_append_only_guard() -> None:
    """Register ORM event listeners that block UPDATE/DELETE on append-only models.

    Safe to call multiple times — only the first call registers listeners.
    """
    global _guard_registered
    if _guard_registered:
        return
    _guard_registered = True

    _register_blocker(AuditEvent, "audit_events")
    _register_blocker(ErrorEvent, "error_events")
