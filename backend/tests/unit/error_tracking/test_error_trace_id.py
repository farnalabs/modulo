"""FAR-198: error events carry the deterministic OTel trace_id."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.error_tracking import _run_trace_id, emit_signal_event
from modulo.otel_bridge import trace_id_for_thread


def test_run_trace_id_matches_api_derivation() -> None:
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    assert _run_trace_id(org_id, run_id) == trace_id_for_thread(f"{org_id}:{run_id}")
    assert _run_trace_id(str(org_id), str(run_id)) == trace_id_for_thread(f"{org_id}:{run_id}")


def test_run_trace_id_none_safe() -> None:
    assert _run_trace_id(None, None) is None
    assert _run_trace_id("not-a-uuid", "not-a-uuid") is None


@pytest.mark.asyncio
async def test_emit_signal_event_context_carries_trace_id() -> None:
    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    session = AsyncMock()
    captured: dict = {}

    async def _fake_create_event(session: Any, **kwargs: object) -> AsyncMock:
        captured.update(kwargs["context_json"])  # type: ignore[arg-type]
        evt = AsyncMock()
        evt.id = uuid.uuid4()
        return evt

    group = AsyncMock()
    group.id = uuid.uuid4()
    group.count = 1

    engine = AsyncMock()
    engine.evaluate = AsyncMock(return_value=[])

    with (
        patch("modulo.core.error_tracking._create_signal_event", new=_fake_create_event),
        patch(
            "modulo.core.error_tracking.get_error_group_by_fingerprint",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("modulo.core.error_tracking.upsert_error_group", new_callable=AsyncMock, return_value=group),
        patch("modulo.core.error_tracking._get_alert_engine", new_callable=AsyncMock, return_value=engine),
    ):
        await emit_signal_event(
            session,
            org_id,
            signal="test.signal",
            pipeline_id=None,
            message="boom",
            level="error",
            run_id=str(run_id),
        )

    assert captured["trace_id"] == trace_id_for_thread(f"{org_id}:{run_id}")
    assert captured["run_id"] == str(run_id)
