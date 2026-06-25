"""Application-layer append-only guard for AuditEvent records.

Provides defense-in-depth via SQLAlchemy ORM event listeners that prevent
any UPDATE or DELETE operations on AuditEvent instances at the ORM level.
This complements the database-level Postgres triggers.

Usage:
    from modulo.core.audit_logger.append_only import register_append_only_guard
    register_append_only_guard()
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm.mapper import Mapper

from modulo.db.models.audit_event import AuditEvent

_log = logging.getLogger(__name__)


def register_append_only_guard() -> None:
    """Register ORM event listeners that block UPDATE/DELETE on AuditEvent.

    Safe to call multiple times — listeners are idempotent.
    """

    @event.listens_for(AuditEvent, "before_update")
    def _block_audit_event_update(
        mapper: Mapper[AuditEvent],
        connection: object,
        target: AuditEvent,
    ) -> None:
        raise RuntimeError(
            f"AuditEvent {target.id} cannot be updated: audit_events are append-only"
        )

    @event.listens_for(AuditEvent, "before_delete")
    def _block_audit_event_delete(
        mapper: Mapper[AuditEvent],
        connection: object,
        target: AuditEvent,
    ) -> None:
        raise RuntimeError(
            f"AuditEvent {target.id} cannot be deleted: audit_events are append-only"
        )

    _log.info("Registered append-only guard on AuditEvent (UPDATE/DELETE blocked)")
