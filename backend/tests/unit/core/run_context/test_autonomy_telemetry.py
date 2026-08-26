"""Unit tests for autonomy telemetry emission (fail-open evidence record)."""

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


async def test_emits_event_with_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
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

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=org_id,
        run_id=run_id,
        gate_id="g1",
        autonomy_level="fully_autonomous",
        gate_outcome="skipped",
        pipeline_id=pipeline_id,
    )

    # RLS context MUST be established on the session before the audit write,
    # otherwise the INSERT into the STRICT-RLS audit_events table is rejected in
    # production and the event silently never lands (the original bug).
    assert set_org.await_count == 1
    assert set_org.call_args.args[1] == org_id
    assert set_ctx.await_count == 1

    assert append.await_count == 1
    _, kwargs = append.call_args
    captured = kwargs
    assert captured["event_type"] == at.AUTONOMY_LEVEL_APPLIED
    assert captured["org_id"] == org_id
    assert captured["resource_type"] == "run"
    assert str(captured["resource_id"]) == str(run_id)
    assert captured["payload_json"]["autonomy_level"] == "fully_autonomous"
    assert captured["payload_json"]["gate_outcome"] == "skipped"
    assert captured["payload_json"]["gate_id"] == "g1"
    assert str(captured["payload_json"]["pipeline_id"]) == str(pipeline_id)


async def test_noop_when_session_factory_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    mock = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", mock)
    set_org = AsyncMock()
    set_ctx = AsyncMock()
    monkeypatch.setattr("modulo.db.rls.set_rls_org", set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", set_ctx)

    await at.emit_autonomy_telemetry(
        None,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="fired",
    )
    assert mock.await_count == 0
    assert set_org.await_count == 0
    assert set_ctx.await_count == 0


async def test_invalid_gate_outcome_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    mock = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", mock)
    set_org = AsyncMock()
    set_ctx = AsyncMock()
    monkeypatch.setattr("modulo.db.rls.set_rls_org", set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", set_ctx)

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="bogus",
    )
    assert mock.await_count == 0
    assert set_org.await_count == 0
    assert set_ctx.await_count == 0


async def test_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(al, "append_audit_event", _boom)
    set_org = AsyncMock()
    set_ctx = AsyncMock()
    monkeypatch.setattr("modulo.db.rls.set_rls_org", set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", set_ctx)

    # Must not raise — telemetry failures must never break a run.
    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="notify_on_complete",
        gate_outcome="auto_approved",
    )

    # RLS context is still set before the (failing) write.
    assert set_org.await_count == 1
    assert set_ctx.await_count == 1


async def test_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import modulo.core.audit_logger as al

    async def _cancel(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(al, "append_audit_event", _cancel)
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())

    # Documented contract: cancellation is NOT a telemetry failure. The
    # broad fail-open handler must never swallow CancelledError, or a run
    # that is being torn down would keep emitting on a dead loop.
    with pytest.raises(asyncio.CancelledError):
        await at.emit_autonomy_telemetry(
            _session_factory,
            org_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            gate_id="g1",
            autonomy_level="manual_approval",
            gate_outcome="fired",
        )


async def test_noop_when_org_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    mock = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", mock)
    set_org = AsyncMock()
    set_ctx = AsyncMock()
    monkeypatch.setattr("modulo.db.rls.set_rls_org", set_org)
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", set_ctx)

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=None,
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="fired",
    )
    assert mock.await_count == 0
    assert set_org.await_count == 0
    assert set_ctx.await_count == 0


async def test_nullable_fields_are_serialized_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    append = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", append)
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=None,
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="fired",
        pipeline_id=None,
    )

    _, kwargs = append.call_args
    assert kwargs["resource_id"] is None
    assert kwargs["payload_json"]["pipeline_id"] is None
    assert kwargs["payload_json"]["human_only"] is False


async def test_human_only_flag_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    import modulo.core.audit_logger as al

    append = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", append)
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="fired",
        human_only=True,
    )

    _, kwargs = append.call_args
    assert kwargs["payload_json"]["human_only"] is True
