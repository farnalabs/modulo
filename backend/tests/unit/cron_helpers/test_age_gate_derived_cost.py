"""FAR-1993: derived-cost attribution for reconcile age-gate runs.

The mid-graph age gate terminal-fails ``no_progress`` runs with a raw UPDATE
that writes NO cost columns, so every such run used to book $0.00 sandbox
spend. The fix attributes the run's HEARTBEAT-PROVEN wall-clock window
(``min(heartbeat_at, completed_at) - started_at``) as derived spend — never the
age bound, which would overstate a sandbox that died early and could falsely
trip the spend ceiling.

These tests cover the liveness-window arithmetic, the per-run attribution
helper (including the fail-open / skip cases) and the post-commit wiring in
``_terminalize_run_facts``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core import cron_helpers as ch
from tests.unit.cron_helpers._helpers import _Begin

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
RUN = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
STARTED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _heartbeat_liveness_seconds — the liveness proof
# ---------------------------------------------------------------------------


class TestHeartbeatLivenessSeconds:
    def test_positive_window_is_heartbeat_minus_started(self) -> None:
        seconds = ch._heartbeat_liveness_seconds(STARTED, STARTED + timedelta(hours=2), STARTED + timedelta(hours=3))
        assert seconds == 7200.0

    def test_heartbeat_null_yields_none(self) -> None:
        """No heartbeat is no liveness proof — never fall back to the age bound."""
        assert ch._heartbeat_liveness_seconds(STARTED, None, STARTED + timedelta(hours=3)) is None

    def test_started_null_yields_none(self) -> None:
        assert ch._heartbeat_liveness_seconds(None, STARTED + timedelta(hours=1), STARTED + timedelta(hours=2)) is None

    def test_heartbeat_equal_to_started_yields_none(self) -> None:
        assert ch._heartbeat_liveness_seconds(STARTED, STARTED, STARTED + timedelta(hours=1)) is None

    def test_heartbeat_before_started_yields_none(self) -> None:
        assert ch._heartbeat_liveness_seconds(STARTED, STARTED - timedelta(minutes=5), STARTED) is None

    def test_completed_before_heartbeat_caps_the_window(self) -> None:
        """Clock skew / a re-stamp past the terminal write caps at completed_at."""
        seconds = ch._heartbeat_liveness_seconds(STARTED, STARTED + timedelta(hours=2), STARTED + timedelta(hours=1))
        assert seconds == 3600.0

    def test_completed_before_started_yields_none(self) -> None:
        """A window that would be negative is not a duration."""
        seconds = ch._heartbeat_liveness_seconds(STARTED, STARTED + timedelta(hours=2), STARTED - timedelta(hours=1))
        assert seconds is None

    def test_missing_completed_at_uses_the_heartbeat(self) -> None:
        seconds = ch._heartbeat_liveness_seconds(STARTED, STARTED + timedelta(minutes=30), None)
        assert seconds == 1800.0


# ---------------------------------------------------------------------------
# _attribute_age_gate_derived_cost — per-run, own session, fail-open
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal AsyncSession double: context manager + one select + begin()."""

    def __init__(self, row: Any) -> None:
        self._row = row
        self.executed: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append(stmt)
        if isinstance(self._row, BaseException):
            raise self._row
        result = MagicMock()
        result.one_or_none.return_value = self._row
        return result


def _row(
    *,
    started_at: datetime | None = STARTED,
    heartbeat_at: datetime | None = STARTED + timedelta(hours=2),
    completed_at: datetime | None = STARTED + timedelta(hours=3),
    code: str | None = ch._NO_PROGRESS_ERROR_CODE,
    detail: str | None = (
        "No node progress within the 135m age bound "
        "(dispatcher_reconcile age gate; node_attempts=1, claims=2, dispatch_phase=unknown)"
    ),
) -> tuple[Any, Any, Any, Any, Any]:
    return (started_at, heartbeat_at, completed_at, code, detail)


async def _run_attribution(
    row: Any,
    *,
    finalize: AsyncMock | None = None,
) -> tuple[AsyncMock, _FakeSession]:
    session = _FakeSession(row)
    finalize_mock = finalize if finalize is not None else AsyncMock()
    with (
        patch.object(ch, "_open_factory", lambda: lambda: session),
        patch.object(ch, "_set_rls_org", new=AsyncMock()),
        patch("modulo.core.cost_controller.finalize.finalize_failed_run", new=finalize_mock),
    ):
        await ch._attribute_age_gate_derived_cost(RUN, ORG)
    return finalize_mock, session


class TestAttributeAgeGateDerivedCost:
    @pytest.mark.asyncio
    async def test_attributes_heartbeat_window_with_the_gate_label(self) -> None:
        finalize, _ = await _run_attribution(_row())
        finalize.assert_awaited_once()
        kwargs = finalize.await_args.kwargs
        assert kwargs["run_id"] == RUN
        assert kwargs["org_id"] == ORG
        assert kwargs["derived_sandbox_wall_hours"] == pytest.approx(2.0)
        assert kwargs["derived_from"] == "reconcile_age_gate_heartbeat"
        # The gate's OWN code/detail are re-read and passed through verbatim.
        assert kwargs["error_code"] == ch._NO_PROGRESS_ERROR_CODE
        assert kwargs["error_detail"].startswith("No node progress within the 135m age bound")

    @pytest.mark.asyncio
    async def test_null_heartbeat_skips_and_writes_no_cost(self) -> None:
        finalize, _ = await _run_attribution(_row(heartbeat_at=None))
        finalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_not_after_started_skips(self) -> None:
        finalize, _ = await _run_attribution(_row(heartbeat_at=STARTED - timedelta(minutes=1)))
        finalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_completed_before_heartbeat_caps_the_attributed_window(self) -> None:
        finalize, _ = await _run_attribution(_row(completed_at=STARTED + timedelta(hours=1)))
        finalize.assert_awaited_once()
        assert finalize.await_args.kwargs["derived_sandbox_wall_hours"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_missing_row_skips(self) -> None:
        finalize, _ = await _run_attribution(None)
        finalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_relabelled_by_another_writer_is_not_overwritten(self) -> None:
        finalize, _ = await _run_attribution(_row(code="executor_stalled"))
        finalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_finalize_failure_is_swallowed(self) -> None:
        """Fail-open per run: a finalize raise must not propagate out of the helper."""
        finalize = AsyncMock(side_effect=RuntimeError("boom"))
        # No raise expected — the helper logs and returns.
        await _run_attribution(_row(), finalize=finalize)
        finalize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finalize_failure_for_one_run_still_attributes_the_second(self) -> None:
        """One run's finalize raising must not block the next run's attribution.

        Run A's ``finalize_failed_run`` raises; run B's must still be called with
        its own window, and nothing may propagate out of either helper call.
        """
        run_b = uuid.uuid4()
        seen: list[uuid.UUID] = []

        async def _finalize(session: Any, **kwargs: Any) -> None:
            seen.append(kwargs["run_id"])
            if kwargs["run_id"] == RUN:
                raise RuntimeError("boom")

        with (
            patch.object(ch, "_open_factory", lambda: lambda: _FakeSession(_row())),
            patch.object(ch, "_set_rls_org", new=AsyncMock()),
            patch("modulo.core.cost_controller.finalize.finalize_failed_run", new=_finalize),
        ):
            await ch._attribute_age_gate_derived_cost(RUN, ORG)
            await ch._attribute_age_gate_derived_cost(run_b, ORG)

        assert seen == [RUN, run_b]

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self) -> None:
        finalize = AsyncMock(side_effect=asyncio.CancelledError())
        session = _FakeSession(_row())
        with (
            patch.object(ch, "_open_factory", lambda: lambda: session),
            patch.object(ch, "_set_rls_org", new=AsyncMock()),
            patch("modulo.core.cost_controller.finalize.finalize_failed_run", new=finalize),
            pytest.raises(asyncio.CancelledError),
        ):
            await ch._attribute_age_gate_derived_cost(RUN, ORG)


# ---------------------------------------------------------------------------
# post-commit wiring in _terminalize_run_facts
# ---------------------------------------------------------------------------


class TestTerminalizeRunFactsWiring:
    @pytest.mark.asyncio
    async def test_each_age_gate_id_is_attributed_post_commit(self) -> None:
        run_b = uuid.uuid4()
        attributed: list[tuple[uuid.UUID, uuid.UUID]] = []

        async def _record(run_id: uuid.UUID, org_id: uuid.UUID) -> None:
            attributed.append((run_id, org_id))

        with (
            patch.object(ch, "_record_fact_for_terminalized_run", new=AsyncMock()),
            patch.object(ch, "_record_terminalisation_audits", new=AsyncMock()),
            patch.object(ch, "_attribute_age_gate_derived_cost", new=_record),
        ):
            await ch._terminalize_run_facts(
                terminalized_run_ids=[(RUN, ORG), (run_b, ORG)],
                age_gate_run_ids=[(RUN, ORG), (run_b, ORG)],
                facts_max=25,
                summary=ch._dispatcher_summary(),
                stage=None,
            )

        assert attributed == [(RUN, ORG), (run_b, ORG)]
