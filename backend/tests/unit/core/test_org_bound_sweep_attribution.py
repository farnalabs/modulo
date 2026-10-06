"""FAR-1501 (iteration 2) — per-org ERROR attribution for the remaining bound sweeps.

``ErrorTrackingLogHandler`` persists an ERROR record only when ``org_id_var``
is bound at log time; system crons bind ``None`` at the job level, so every
per-org loop inside them must bind its own org for the duration of the tick.
These tests drive the REAL entry points (real ``_bound_org`` loop binds, real
``ErrorTrackingLogHandler``) and never set ``org_id_var`` themselves — the org
on each forwarded record comes solely from the production bind:

1. ``hitl_overdue`` (``dispatch_overdue_notifications``) — a per-entry
   notification failure is attributed to the org of its tick;
2. ``hitl_deadline_warning`` (``dispatch_deadline_notifications``) — a
   per-org webhook-subscriber check failure is attributed likewise;
3. a parametrised set proving three MORE wrapped sites self-attribute:
   the claim-expiry audit-write failure, ``cost_probe.org_failed``, and a
   failure inside a ``_run_sweep_orgs`` tick.

Session I/O is doubled at the statement boundary (content-dispatched doubles);
the org collection, the loop binds, the contextvar flow and the handler are
real. Each test also asserts the caller context is clean afterwards.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var

ORG1 = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
ORG2 = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit``."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self.orgs: list[str | None] = []

    async def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.orgs.append(org_id_var.get())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Rows(list[Any]):
    """List that answers the result-shape methods the sweeps call."""

    def all(self) -> _Rows:
        return self

    def scalars(self) -> _Rows:
        return self

    def scalar_one(self) -> Any:
        return self[0]

    def scalar_one_or_none(self) -> Any | None:
        return self[0] if self else None


class _OrgSweepSession:
    """Statement-dispatch session double shared by the sweeps under test.

    ``get_bind`` reports postgresql so the REAL ``set_rls_org`` exercises the
    production ``set_config`` path; ``in_transaction`` is True so its guard
    passes. Every statement is matched on its rendered SQL — an unrecognised
    statement raises loudly rather than silently passing a wrong branch.
    """

    def __init__(
        self,
        org_ids: list[uuid.UUID],
        *,
        entries: list[tuple[Any, str]] | None = None,
        stale_rows: list[Any] | None = None,
    ) -> None:
        self._org_ids = org_ids
        self._entries = entries or []
        self._stale = stale_rows or []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    def begin_nested(self) -> _Begin:
        return _Begin()

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> MagicMock:
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        return bind

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        rendered = str(stmt)
        if "set_config" in rendered:
            return MagicMock()
        if "pg_try_advisory_xact_lock" in rendered:
            result = MagicMock()
            result.scalar_one.return_value = True
            return result
        if "FROM organisations" in rendered:
            result = MagicMock()
            result.scalars.return_value = _Rows(self._org_ids)
            return result
        if "UPDATE" in rendered:
            return MagicMock()
        if "JOIN" in rendered:
            result = MagicMock()
            result.all.return_value = _Rows(self._entries)
            return result
        if "FROM hitl_claims" in rendered:
            result = MagicMock()
            result.all.return_value = _Rows(self._stale)
            return result
        raise AssertionError(f"unexpected statement in sweep double:\n{rendered}")


@pytest.fixture(autouse=True)
def _clean_rate_limit_state() -> Iterator[None]:
    """The handler forwards at most one record per org per 5s window."""
    ErrorTrackingLogHandler._last_write_time.clear()
    yield
    ErrorTrackingLogHandler._last_write_time.clear()


@pytest.fixture
def sink() -> _RecordingSink:
    return _RecordingSink()


@pytest.fixture
def capture(sink: _RecordingSink) -> Iterator[None]:
    """Attach a REAL ``ErrorTrackingLogHandler`` (with a recording sink)."""
    handler = ErrorTrackingLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield
    finally:
        root.removeHandler(handler)


async def _drain() -> None:
    """Let the handler's forwarding task run before the sink is read."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_hitl_overdue_notification_failures_are_attributed_to_their_own_orgs(
    capture: None,
    sink: _RecordingSink,
) -> None:
    """``hitl_overdue`` attributes each per-org dispatch failure to its org.

    Two orgs; the notifier raises for every entry, so
    ``hitl.overdue_job.notification_failed`` is logged once per org inside the
    real bound tick. This test never touches ``org_id_var``: the two distinct
    orgs on the forwarded records prove both attribution AND that the second
    tick did not inherit the first org's context.
    """
    from modulo.core.hitl_manager import overdue_warning as ow

    assert org_id_var.get() is None
    claim = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        review_id="gate-1",
        claimed_at=datetime.now(UTC) - timedelta(hours=5),
    )
    session = _OrgSweepSession([ORG1, ORG2], entries=[(claim, "Pipe")])
    factory = MagicMock(return_value=session)
    notifier = MagicMock()
    notifier.dispatch_event = AsyncMock(side_effect=RuntimeError("smtp down"))

    result = await ow.dispatch_overdue_notifications(factory, notifier=notifier)
    await _drain()

    assert not result
    assert len(sink.messages) == 2
    assert all("hitl.overdue_job.notification_failed" in message for message in sink.messages)
    assert sink.orgs == [str(ORG1), str(ORG2)]
    assert org_id_var.get() is None


async def test_hitl_deadline_failures_are_attributed_to_their_own_orgs(
    capture: None,
    sink: _RecordingSink,
) -> None:
    """``hitl_deadline_warning`` attributes each per-org failure to its org.

    Two orgs; ``Notifier.has_subscribers`` raises so
    ``hitl.deadline_warning.webhook_subscriber_check_failed`` is logged once
    per org inside the real bound tick, and the entry is skipped (recipients
    resolve empty). No hand-set ``org_id_var``: the orgs on the forwarded
    records come solely from the production bind.
    """
    from modulo.core.hitl_manager import deadline_warning as dw

    assert org_id_var.get() is None
    now = datetime.now(UTC)
    claim = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        review_id="gate-1",
        pipeline_id=uuid.uuid4(),
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(minutes=10),
        gate_config_json=None,
    )
    session = _OrgSweepSession([ORG1, ORG2], entries=[(claim, "Pipe")])
    factory = MagicMock(return_value=session)
    notifier = MagicMock()
    notifier.has_subscribers = AsyncMock(side_effect=RuntimeError("subs down"))

    with patch.object(dw, "resolve_hitl_email_recipients", AsyncMock(return_value=[])):
        result = await dw.dispatch_deadline_notifications(
            factory,
            grace_seconds=0,
            redis_client=AsyncMock(),
            notifier=notifier,
        )
    await _drain()

    assert not result
    assert len(sink.messages) == 2
    assert all("webhook_subscriber_check_failed" in message for message in sink.messages)
    assert sink.orgs == [str(ORG1), str(ORG2)]
    assert org_id_var.get() is None


@pytest.mark.parametrize(
    "site",
    ["expiry_audit", "cost_probe", "trigger_streak"],
)
async def test_additional_bound_sites_attribute_their_per_org_errors(
    site: str,
    capture: None,
    sink: _RecordingSink,
) -> None:
    """Three more wrapped sites self-attribute without hand-set context.

    - ``expiry_audit``: the real ``expire_stale_claims`` loop — a failed
      ``append_audit_event`` (caught per org) must be attributed to that org;
    - ``cost_probe``: the real ``run_probe`` loop — ``cost_probe.org_failed``
      fires once per org when ``_probe_org`` raises;
    - ``trigger_streak``: a failure emitted inside a real
      ``_run_sweep_orgs`` tick (the per-org worker is stubbed — what is under
      test is the bind around the tick, since the production CRITICAL sites
      live deep in the notify chain).
    """
    assert org_id_var.get() is None

    if site == "expiry_audit":
        from modulo.core.hitl_manager import expiry_job as exp

        row = SimpleNamespace(id=uuid.uuid4(), run_id=uuid.uuid4(), review_id="gate-1", account_id=uuid.uuid4())
        session = _OrgSweepSession([ORG1, ORG2], stale_rows=[row])
        factory = MagicMock(return_value=session)
        with patch.object(exp, "append_audit_event", AsyncMock(side_effect=RuntimeError("audit down"))):
            expired = await exp.expire_stale_claims(factory, notifier=None)
        await _drain()
        assert len(expired) == 2
        expected = "Failed to record claim_expired audit event"
    elif site == "cost_probe":
        from modulo.core.cost_controller import probe as probe_mod

        session = _OrgSweepSession([ORG1, ORG2])
        factory = MagicMock(return_value=session)
        with patch.object(probe_mod, "_probe_org", new=AsyncMock(side_effect=RuntimeError("probe down"))):
            summary = await probe_mod.run_probe(factory)
        await _drain()
        assert summary["orgs_failed"] == 2
        expected = "cost_probe.org_failed"
    else:
        from modulo.core import trigger_streak as ts

        async def _stub(_factory: Any, _org_id: uuid.UUID, **_kwargs: Any) -> int:
            ts._log.exception("trigger_streak.test_org_failure")
            return 0

        summary = {"budget_exceeded": False}
        with patch.object(ts, "_enforce_org_streak", new=_stub):
            await ts._run_sweep_orgs(
                MagicMock(),
                [ORG1, ORG2],
                None,
                10,
                time.monotonic() + 30,
                summary,
            )
        await _drain()
        expected = "trigger_streak.test_org_failure"

    assert len(sink.messages) == 2
    assert all(expected in message for message in sink.messages)
    assert sink.orgs == [str(ORG1), str(ORG2)]
    assert org_id_var.get() is None
