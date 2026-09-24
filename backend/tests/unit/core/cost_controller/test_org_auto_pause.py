"""Unit tests for the FAR-1183 org cost-controls auto-pause (org_auto_pause).

Covers the toggle gate, the idempotent heuristic (never auto-unpauses), the
fail-open envelope, and the payload written into the audit event.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller import org_auto_pause
from modulo.core.cost_controller.org_auto_pause import (
    AUTO_PAUSE_REASON_DAILY_LIMIT,
    AUTO_PAUSE_REASON_SPEND_CEILING,
    ORG_TRIGGERS_AUTO_PAUSED_EVENT,
    circuit_breaker_enabled_for_org,
    maybe_auto_pause_org_triggers,
)
from modulo.core.notifier import EVENT_ORG_TRIGGERS_AUTO_PAUSED
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


def test_reason_labels_cover_both_reasons() -> None:
    from modulo.core.cost_controller.org_auto_pause import _REASON_LABELS

    assert _REASON_LABELS[AUTO_PAUSE_REASON_DAILY_LIMIT]
    assert _REASON_LABELS[AUTO_PAUSE_REASON_SPEND_CEILING]


def test_cents_str_formats_cents_and_none() -> None:
    assert org_auto_pause._cents_str(1500) == "15.00"
    assert org_auto_pause._cents_str(None) == "0.00"


def _patched_notifier(dispatch_event: AsyncMock):
    """Patch the Notifier construction seams ``_notify_admins`` imports lazily."""
    notifier = MagicMock()
    notifier.dispatch_event = dispatch_event
    return (
        patch("modulo.core.notifier.Notifier", return_value=notifier),
        patch("modulo.db.session.get_shared_engine", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test-key")),
        notifier,
    )


async def test_notify_admins_dispatches_real_notification() -> None:
    """The real ``_notify_admins`` builds the payload and dispatches the event."""
    org = _org(circuit_breaker_enabled=True)
    session = AsyncMock()
    run_id = uuid.uuid4()

    notifier_patch, engine_patch, settings_patch, notifier = _patched_notifier(AsyncMock())
    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock()),
        notifier_patch,
        engine_patch,
        settings_patch,
    ):
        engaged = await maybe_auto_pause_org_triggers(
            session,
            org=org,
            reason=AUTO_PAUSE_REASON_DAILY_LIMIT,
            spend_cents=1000,
            limit_cents=None,
            run_id=run_id,
        )

    assert engaged is True
    notifier.dispatch_event.assert_awaited_once()
    args, kwargs = notifier.dispatch_event.await_args
    assert args[0] == org.id
    assert args[1] == EVENT_ORG_TRIGGERS_AUTO_PAUSED
    assert args[2]["reason_label"] == "the daily spend limit was exceeded"
    assert args[2]["spend_usd"] == "10.00"
    assert args[2]["limit_usd"] == "0.00"
    assert kwargs["run_id"] == run_id


async def test_notify_admins_swallows_dispatch_failure() -> None:
    """A notification failure is logged + swallowed; the pause still succeeds."""
    org = _org(circuit_breaker_enabled=True)
    session = AsyncMock()

    notifier_patch, engine_patch, settings_patch, notifier = _patched_notifier(
        AsyncMock(side_effect=RuntimeError("boom"))
    )
    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock()),
        notifier_patch,
        engine_patch,
        settings_patch,
    ):
        engaged = await maybe_auto_pause_org_triggers(
            session, org=org, reason=AUTO_PAUSE_REASON_SPEND_CEILING, spend_cents=1500, limit_cents=1000
        )

    assert engaged is True
    notifier.dispatch_event.assert_awaited_once()


async def test_notify_admins_cancellation_propagates() -> None:
    """Cancellation must not be swallowed — it propagates through both handlers."""
    org = _org(circuit_breaker_enabled=True)
    session = AsyncMock()

    notifier_patch, engine_patch, settings_patch, _ = _patched_notifier(AsyncMock(side_effect=asyncio.CancelledError))
    with (
        patch.object(org_auto_pause, "append_audit_event", new=AsyncMock()),
        notifier_patch,
        engine_patch,
        settings_patch,
        pytest.raises(asyncio.CancelledError),
    ):
        await maybe_auto_pause_org_triggers(
            session, org=org, reason=AUTO_PAUSE_REASON_DAILY_LIMIT, spend_cents=1000, limit_cents=500
        )
