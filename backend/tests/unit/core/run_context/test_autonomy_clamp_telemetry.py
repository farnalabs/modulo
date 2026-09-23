"""Unit tests for the clamp audit event (run.autonomy_recommendation_clamped).

Mirrors test_autonomy_telemetry.py: fail-open emission with the RLS
org/execution-context transaction shape, CancelledError re-raised, and a
payload carrying gate_id / requested / effective / ceiling (FAR-1163 S0).
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.run_context import autonomy_telemetry as at

pytestmark = pytest.mark.asyncio


class _NullBegin:
    """Async context manager returned by ``_NullSession.begin()``."""

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _NullSession:
    """Minimal session stand-in: supports ``async with session_factory() as
    session, session.begin():`` without a real DB."""

    def begin(self) -> Any:
        return _NullBegin()

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _session_factory() -> Any:
    return _NullSession()


async def test_clamp_emits_event_with_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    append = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", append)
    set_org = AsyncMock()
    set_ctx = AsyncMock()
    monkeypatch.setattr("modulo.db.rls.set_rls_org", set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", set_ctx)

    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    await at.emit_autonomy_clamp_telemetry(
        _session_factory,
        org_id=org_id,
        run_id=run_id,
        gate_id="g1",
        requested="fully_autonomous",
        effective="manual_approval",
        ceiling="manual_approval",
        pipeline_id=pipeline_id,
    )

    # RLS context MUST be established before the audit write (same contract
    # as emit_autonomy_telemetry — see the sibling test file).
    assert set_org.await_count == 1
    assert set_org.call_args.args[1] == org_id
    assert set_ctx.await_count == 1

    assert append.await_count == 1
    kwargs = append.call_args.kwargs
    assert kwargs["event_type"] == at.AUTONOMY_RECOMMENDATION_CLAMPED
    assert kwargs["event_type"] == "run.autonomy_recommendation_clamped"
    assert kwargs["org_id"] == org_id
    assert kwargs["resource_type"] == "run"
    assert str(kwargs["resource_id"]) == str(run_id)
    payload = kwargs["payload_json"]
    assert payload["gate_id"] == "g1"
    assert payload["requested"] == "fully_autonomous"
    assert payload["effective"] == "manual_approval"
    assert payload["ceiling"] == "manual_approval"
    assert str(payload["pipeline_id"]) == str(pipeline_id)
    assert payload["actor"] == "system"
    summary = payload["summary"]
    assert "clamped" in summary
    assert "gate g1" in summary

    # The event type must be registered so it flows through product-analytics
    # ingest (asserted here rather than in a sync test under the module-level
    # asyncio mark).
    from modulo.core.product_analytics.metrics_constants import VALID_EVENT_TYPES

    assert at.AUTONOMY_RECOMMENDATION_CLAMPED in VALID_EVENT_TYPES
    assert "run.autonomy_recommendation_clamped" in VALID_EVENT_TYPES


async def test_clamp_noop_when_session_factory_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    mock = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", mock)

    await at.emit_autonomy_clamp_telemetry(
        None,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        requested="fully_autonomous",
        effective="manual_approval",
        ceiling="manual_approval",
    )
    assert mock.await_count == 0


async def test_clamp_noop_when_org_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    mock = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", mock)

    await at.emit_autonomy_clamp_telemetry(
        _session_factory,
        org_id=None,
        run_id=uuid.uuid4(),
        gate_id="g1",
        requested="fully_autonomous",
        effective="manual_approval",
        ceiling="manual_approval",
    )
    assert mock.await_count == 0


async def test_clamp_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(al, "append_audit_event", _boom)
    set_org = AsyncMock()
    set_ctx = AsyncMock()
    monkeypatch.setattr("modulo.db.rls.set_rls_org", set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", set_ctx)

    # Must not raise — telemetry failures must never break a run.
    await at.emit_autonomy_clamp_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        requested="notify_on_complete",
        effective="manual_approval",
        ceiling="manual_approval",
    )

    assert set_org.await_count == 1
    assert set_ctx.await_count == 1


async def test_clamp_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import modulo.core.audit_logger as al

    async def _cancel(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(al, "append_audit_event", _cancel)
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())

    # Documented contract: cancellation is NOT a telemetry failure.
    with pytest.raises(asyncio.CancelledError):
        await at.emit_autonomy_clamp_telemetry(
            _session_factory,
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            gate_id="g1",
            requested="fully_autonomous",
            effective="manual_approval",
            ceiling="manual_approval",
        )
