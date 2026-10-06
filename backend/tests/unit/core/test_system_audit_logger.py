"""Unit tests for the org-independent system audit writer (FAR-1517).

The integration suite proves the durable rows survive a hard delete on real
Postgres; these tests pin the writer's own branch behaviour — the fail-closed
blank-event guard and the optional org/actor columns — without a database.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.system_audit_logger import append_system_audit_event


def _session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    return session


async def test_blank_event_type_is_rejected_before_any_write() -> None:
    session = _session()

    with pytest.raises(ValueError, match="non-empty event_type"):
        await append_system_audit_event(session, event_type="   ")

    session.execute.assert_not_awaited()


async def test_absent_org_and_actor_are_not_synthesised() -> None:
    session = _session()

    await append_system_audit_event(session, event_type="org_deletion_completed")

    params = session.execute.await_args.args[0].compile().params
    assert params["org_id"] is None
    assert params["actor_user_id"] is None
    assert params["payload_json"] == {}


async def test_present_org_and_actor_are_recorded_in_payload() -> None:
    session = _session()
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    await append_system_audit_event(
        session,
        event_type="org_deletion_requested",
        org_id=org_id,
        actor_user_id=actor_id,
    )

    params = session.execute.await_args.args[0].compile().params
    assert params["payload_json"]["organisation_id"] == str(org_id)
    assert params["payload_json"]["actor_user_id"] == str(actor_id)
