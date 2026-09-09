"""Unit tests for the per-machine runner health probe (FAR-591, D5).

Covers the PURE mapping helpers (cache-state -> strip-state, worst-of
aggregation, staleness computation) and the orchestration (engine probe ->
per-(org, machine) upsert -> healthy-to-unreachable transition emission)
with a fake engine boundary + fake session factory.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.bundled_runner.health_probe import (
    EngineProbeOutcome,
    aggregate_strip_state,
    run_runner_health_probe,
    strip_state_for_row,
)
from modulo.db.crud.runner_probe import (
    PROBE_INTERVAL_SECONDS,
    PROBE_STALENESS_THRESHOLD_SECONDS,
    probe_age_seconds,
    probe_is_stale,
)

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_IMAGE_REF = "modulo-runner:opencode@sha256:" + "1" * 64


def _probe(seconds_ago: int) -> datetime:
    return _NOW - timedelta(seconds=seconds_ago)


class TestStalenessComputation:
    def test_interval_is_60s_per_adr_029(self) -> None:
        assert PROBE_INTERVAL_SECONDS == 60

    def test_staleness_threshold_is_2x_interval(self) -> None:
        assert PROBE_STALENESS_THRESHOLD_SECONDS == 2 * PROBE_INTERVAL_SECONDS

    def test_fresh_result_is_not_stale(self) -> None:
        assert probe_is_stale(_probe(30), _NOW) is False

    def test_exactly_at_threshold_is_not_stale(self) -> None:
        assert probe_is_stale(_probe(PROBE_STALENESS_THRESHOLD_SECONDS), _NOW) is False

    def test_past_threshold_is_stale(self) -> None:
        assert probe_is_stale(_probe(PROBE_STALENESS_THRESHOLD_SECONDS + 1), _NOW) is True

    def test_age_never_negative(self) -> None:
        assert probe_age_seconds(_NOW + timedelta(seconds=10), _NOW) == 0

    def test_age_whole_seconds(self) -> None:
        assert probe_age_seconds(_probe(90), _NOW) == 90


class TestStripStateForRow:
    def test_healthy_when_reachable_and_images_present(self) -> None:
        state = strip_state_for_row(engine_reachable=True, images_present=True, probed_at=_probe(30), now=_NOW)
        assert state == "healthy"

    def test_engine_unreachable_dominates_images(self) -> None:
        state = strip_state_for_row(engine_reachable=False, images_present=None, probed_at=_probe(30), now=_NOW)
        assert state == "engine_unreachable"

    def test_image_not_pulled_when_engine_reachable_but_image_missing(self) -> None:
        state = strip_state_for_row(engine_reachable=True, images_present=False, probed_at=_probe(30), now=_NOW)
        assert state == "image_not_pulled"

    def test_dead_probe_reads_stale_never_healthy(self) -> None:
        """A probe suspended past the threshold renders 'status unknown'."""
        state = strip_state_for_row(engine_reachable=True, images_present=True, probed_at=_probe(600), now=_NOW)
        assert state == "stale"


class TestAggregateStripState:
    def test_empty_reads_stale(self) -> None:
        """No cached rows at all â€” the strip must read unknown, never green."""
        assert aggregate_strip_state([]) == "stale"

    def test_all_healthy_reads_healthy(self) -> None:
        assert aggregate_strip_state(["healthy", "healthy"]) == "healthy"

    def test_stale_dominates(self) -> None:
        assert aggregate_strip_state(["healthy", "stale"]) == "stale"

    def test_engine_unreachable_dominates_image_not_pulled(self) -> None:
        assert aggregate_strip_state(["image_not_pulled", "engine_unreachable"]) == "engine_unreachable"

    def test_image_not_pulled_dominates_healthy(self) -> None:
        assert aggregate_strip_state(["healthy", "image_not_pulled"]) == "image_not_pulled"


def _fake_session_factory(session: AsyncMock) -> Any:
    class _Factory:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> AsyncMock:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    return _Factory()


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _fake_boundary(reachable: bool, image_ok: bool = True) -> Any:
    boundary = MagicMock()
    if reachable:
        outcome = EngineProbeOutcome(
            reachable=True,
            engine_info={"cpu_count": 8, "mem_total_mb": 16384},
        )
    else:
        outcome = EngineProbeOutcome(reachable=False, error="connection refused")
    boundary.probe_engine = AsyncMock(return_value=outcome)
    boundary.image_present = AsyncMock(return_value=image_ok)
    boundary.close = AsyncMock()
    return boundary


class TestRunRunnerHealthProbe:
    @pytest.mark.asyncio
    async def test_reachable_engine_upserts_healthy_rows_per_org(self) -> None:
        session = _make_session()
        boundary = _fake_boundary(reachable=True, image_ok=True)
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[_IMAGE_REF])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()) as upsert,
            patch("modulo.core.bundled_runner.health_probe._emit_unreachable_transition", new=AsyncMock()) as emit,
        ):
            result = await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        assert result["reachable"] is True
        assert result["orgs_probed"] == 1
        assert result["transitions"] == 0
        upsert.assert_awaited_once()
        kwargs = upsert.await_args.kwargs
        assert kwargs["engine_reachable"] is True
        assert kwargs["images_present"] is True
        assert kwargs["engine_info"] == {"cpu_count": 8, "mem_total_mb": 16384}
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreachable_engine_writes_null_image_state(self) -> None:
        session = _make_session()
        boundary = _fake_boundary(reachable=False)
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[_IMAGE_REF])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()) as upsert,
            patch("modulo.core.bundled_runner.health_probe._emit_unreachable_transition", new=AsyncMock()),
        ):
            await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        kwargs = upsert.await_args.kwargs
        assert kwargs["engine_reachable"] is False
        assert kwargs["images_present"] is None
        assert "connection refused" in kwargs["probe_error"]

    @pytest.mark.asyncio
    async def test_healthy_to_unreachable_transition_emits_error_and_notification(self) -> None:
        session = _make_session()
        boundary = _fake_boundary(reachable=False)
        previous = MagicMock()
        previous.engine_reachable = True
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=previous)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()),
            patch("modulo.core.bundled_runner.health_probe._emit_unreachable_transition", new=AsyncMock()) as emit,
        ):
            result = await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        assert result["transitions"] == 1
        emit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unreachable_to_unreachable_does_not_re_emit(self) -> None:
        session = _make_session()
        boundary = _fake_boundary(reachable=False)
        previous = MagicMock()
        previous.engine_reachable = False
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=previous)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()),
            patch("modulo.core.bundled_runner.health_probe._emit_unreachable_transition", new=AsyncMock()) as emit,
        ):
            result = await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        assert result["transitions"] == 0
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_org_failure_is_fail_open(self) -> None:
        """One broken org never aborts the whole tick (other orgs still probed)."""
        session = _make_session()
        boundary = _fake_boundary(reachable=True)
        other_org = uuid.UUID("00000000-0000-0000-0000-000000000002")

        async def _list_orgs(s: Any) -> list[uuid.UUID]:
            return [_ORG_ID, other_org]

        calls: dict[str, int] = {"n": 0}

        async def _upsert(_session: Any, **kwargs: Any) -> None:
            if kwargs["org_id"] == _ORG_ID:
                raise RuntimeError("boom")
            calls["n"] += 1

        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(side_effect=_list_orgs)),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock(side_effect=_upsert)),
        ):
            result = await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        assert result["orgs_probed"] == 1
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_placeholder_digest_image_skips_inspect(self) -> None:
        """The un-landed placeholder digest never hits the engine inspect."""
        session = _make_session()
        boundary = _fake_boundary(reachable=True)
        placeholder_ref = "modulo-runner:opencode@sha256:" + "0" * 64
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[placeholder_ref])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()) as upsert,
        ):
            await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        boundary.image_present.assert_not_awaited()
        kwargs = upsert.await_args.kwargs
        assert kwargs["image_checks"][placeholder_ref] is None
