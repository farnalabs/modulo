"""FAR-1163 regression guard: payload construction is inside the fail-open envelope.

The telemetry refactor moved the ``labels`` import and ``payload_json``
construction outside the guarded region of ``emit_autonomy_telemetry`` — a
raise there would propagate into gate evaluation and break a run, violating
"telemetry must never break a run". These tests prove both emitters swallow a
payload-construction failure (the clamp twin lives in
``test_autonomy_clamp_telemetry.py``).
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
    """Minimal session stand-in for ``session_factory() as s, s.begin():``."""

    def begin(self) -> Any:
        return _NullBegin()

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _session_factory() -> Any:
    return _NullSession()


async def test_emit_payload_construction_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise while building the payload must be swallowed, not propagated.

    Fails without the guarded region: ``short_id`` blowing up during the
    f-string summary build would raise out of ``emit_autonomy_telemetry`` and
    into the HITL gate evaluation that called it.
    """
    import modulo.core.audit_logger as al
    import modulo.core.audit_logger.labels as labels

    append = AsyncMock()
    monkeypatch.setattr(al, "append_audit_event", append)
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())

    def _boom_short_id(run_id: object) -> str:
        raise RuntimeError("labels module broken")

    monkeypatch.setattr(labels, "short_id", _boom_short_id)

    # Must not raise — payload construction is inside the fail-open envelope.
    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="fired",
    )

    assert append.await_count == 0
