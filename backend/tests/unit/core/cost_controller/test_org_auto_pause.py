"""Unit tests for the FAR-1183 org cost-controls auto-pause (org_auto_pause).

Covers the toggle gate, the idempotent heuristic (never auto-unpauses), the
fail-open envelope, and the payload written into the audit event.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.cost_controller import org_auto_pause
from modulo.core.cost_controller.org_auto_pause import (
    AUTO_PAUSE_REASON_DAILY_LIMIT,
    AUTO_PAUSE_REASON_SPEND_CEILING,
    ORG_TRIGGERS_AUTO_PAUSED_EVENT,
    circuit_breaker_enabled_for_org,
    maybe_auto_pause_org_triggers,
)
from modulo.db.models.organisation import Organisation


def _org(
    *,
    circuit_breaker_enabled=False,
    triggers_paused=False,
):
    org = MagicMock(spec=Organisation)
    org.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    org.triggers_paused = triggers_paused
    org.triggers_paused_at = None
    org.settings_json = (
        {"cost_controls": {"circuit_breaker_enabled": circuit_breaker_enabled}} if circuit_breaker_enabled else {}
    )
    return org


def test_readout_toggle_variants() -> None:
    assert circuit_breaker_enabled_for_org(_org(circuit_breaker_enabled=True)) is True
    assert circuit_breaker_enabled_for_org(_org(circuit_breaker_enabled=False)) is False
    org_none = MagicMock(spec=Organisation)
    org_none.settings_json = None
    assert circuit_breaker_enabled_for_org(org_none) is False
    raft = MagicMock(spec=Organisation)
    raft.settings_json = {"cost_controls": "corrupted"}
    assert circuit_breaker_enabled_for_org(raft) is False


async def test_toggle_off_is_strict_noop() -> None:
    org = _org(circuit_breaker_enabled=False)
    session = AsyncMock()

    engaged = await maybe_auto_pause_org_triggers(
        session, org=org, reason=AUTO_PAUSE_REASON_DAILY_LIMIT, spend_cents=1000, limit_cents=500
    )

    assert engaged is False
    assert org.triggers_paused is False
    session.flush.assert_not_called()


async def test_toggle_on_pauses_writes_audit_and_notifies() -> None:
    org = _org(circuit_breaker_enabled=True)
    session = AsyncMock()

    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock()) as audit,
        patch.object(org_auto_pause, "_notify_admins", new=AsyncMock()) as notify,
    ):
        engaged = await maybe_auto_pause_org_triggers(
            session,
            org=org,
            reason=AUTO_PAUSE_REASON_SPEND_CEILING,
            spend_cents=1500,
            limit_cents=1000,
            run_id=None,
        )

    assert engaged is True
    assert org.triggers_paused is True
    assert org.triggers_paused_at is not None
    session.flush.assert_awaited()
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_type"] == ORG_TRIGGERS_AUTO_PAUSED_EVENT
    assert audit.await_args.kwargs["payload_json"]["reason"] == AUTO_PAUSE_REASON_SPEND_CEILING
    assert audit.await_args.kwargs["payload_json"]["spend_cents"] == 1500
    assert audit.await_args.kwargs["payload_json"]["limit_cents"] == 1000
    notify.assert_awaited_once()


async def test_already_paused_is_noop_and_never_unpauses() -> None:
    org = _org(circuit_breaker_enabled=True, triggers_paused=True)
    session = AsyncMock()

    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock()) as audit,
        patch.object(org_auto_pause, "_notify_admins", new=AsyncMock()) as notify,
    ):
        engaged = await maybe_auto_pause_org_triggers(
            session, org=org, reason=AUTO_PAUSE_REASON_DAILY_LIMIT, spend_cents=1000, limit_cents=500
        )

    assert engaged is False
    audit.assert_not_awaited()
    notify.assert_not_awaited()


async def test_audit_failure_is_fail_open() -> None:
    """A pause assist-failure never raises — the caller's terminal write is safe."""
    org = _org(circuit_breaker_enabled=True)
    session = AsyncMock()

    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock(side_effect=RuntimeError("boom"))) as audit,
        patch.object(org_auto_pause, "_notify_admins", new=AsyncMock()) as notify,
    ):
        engaged = await maybe_auto_pause_org_triggers(
            session, org=org, reason=AUTO_PAUSE_REASON_DAILY_LIMIT, spend_cents=1000, limit_cents=500
        )

    assert engaged is False
    audit.assert_awaited_once()
    notify.assert_not_awaited()


async def test_reason_labels_cover_both_reasons() -> None:
    from modulo.core.cost_controller.org_auto_pause import _REASON_LABELS

    assert _REASON_LABELS[AUTO_PAUSE_REASON_DAILY_LIMIT]
    assert _REASON_LABELS[AUTO_PAUSE_REASON_SPEND_CEILING]
