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
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.bundled_runner.health_probe import (
    EngineProbeOutcome,
    aggregate_image_presence,
    aggregate_strip_state,
    run_runner_health_probe,
    scrub_url_credentials,
    strip_state_for_row,
)
from modulo.db.crud.runner_probe import (
    PROBE_INTERVAL_SECONDS,
    PROBE_RETENTION_SECONDS,
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
        """No cached rows at all — the strip must read unknown, never green."""
        assert aggregate_strip_state([]) == "stale"

    def test_all_healthy_reads_healthy(self) -> None:
        assert aggregate_strip_state(["healthy", "healthy"]) == "healthy"

    def test_stale_dominates(self) -> None:
        assert aggregate_strip_state(["healthy", "stale"]) == "stale"

    def test_engine_unreachable_dominates_image_not_pulled(self) -> None:
        assert aggregate_strip_state(["image_not_pulled", "engine_unreachable"]) == "engine_unreachable"

    def test_image_not_pulled_dominates_healthy(self) -> None:
        assert aggregate_strip_state(["healthy", "image_not_pulled"]) == "image_not_pulled"


class TestAggregateImagePresence:
    def test_empty_is_unknown(self) -> None:
        assert aggregate_image_presence([]) is None

    def test_all_present_is_true(self) -> None:
        assert aggregate_image_presence([True, True]) is True

    def test_absent_dominates(self) -> None:
        assert aggregate_image_presence([True, False]) is False

    def test_unknown_state_keeps_unknown(self) -> None:
        """A transient inspect failure must not read as absent (qa F4)."""
        assert aggregate_image_presence([True, None]) is None


class TestScrubUrlCredentials:
    def test_scrubs_userinfo_in_error_string(self) -> None:
        error = "Cannot connect to host tcp://admin:s3cret@proxy.internal:2375 ssl:default"
        scrubbed = scrub_url_credentials(error)
        assert "s3cret" not in scrubbed
        assert "proxy.internal:2375" in scrubbed

    def test_leaves_urls_without_userinfo_untouched(self) -> None:
        url = "tcp://proxy.internal:2375"
        assert scrub_url_credentials(url) == url

    def test_preserves_non_url_colon_pairs(self) -> None:
        # No "//" before the colon pair — not treated as URL userinfo.
        text = "timeout value 5:5 exceeded"
        assert scrub_url_credentials(text) == text


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
    session.begin_nested = MagicMock(return_value=begin_cm)
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
            patch("modulo.db.crud.runner_probe.prune_stale_runner_probe_rows", new=AsyncMock()),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock(side_effect=_upsert)),
        ):
            result = await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        assert result["orgs_probed"] == 1
        assert result["orgs_failed"] == 0
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_placeholder_only_org_reads_unknown_never_false(self) -> None:
        """A placeholder-only org has no inspectable image — unknown, not a
        permanent false ``image_not_pulled`` (qa F4); the placeholder never
        reaches the engine inspect."""
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
        assert kwargs["images_present"] is None
        assert placeholder_ref not in kwargs["image_checks"]

    @pytest.mark.asyncio
    async def test_transient_inspect_failure_is_unknown_not_absent(self) -> None:
        """A non-404 inspect failure records None (unknown) — the aggregate
        must not read ``image_not_pulled`` from a transient error (qa F4)."""
        session = _make_session()
        boundary = _fake_boundary(reachable=True)
        boundary.image_present = AsyncMock(return_value=None)
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[_IMAGE_REF])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()) as upsert,
        ):
            await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        kwargs = upsert.await_args.kwargs
        assert kwargs["images_present"] is None
        assert kwargs["image_checks"][_IMAGE_REF] is None

    @pytest.mark.asyncio
    async def test_prunes_rows_past_retention_window(self) -> None:
        """qa F8: the tick prunes orphaned (org, machine) rows past the
        24h retention window."""
        session = _make_session()
        boundary = _fake_boundary(reachable=True)
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()),
            patch("modulo.db.crud.runner_probe.prune_stale_runner_probe_rows", new=AsyncMock()) as prune,
        ):
            await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        prune.assert_awaited_once()
        assert prune.await_args.kwargs["retention_seconds"] == PROBE_RETENTION_SECONDS

    @pytest.mark.asyncio
    async def test_placeholder_digest_image_skips_inspect(self) -> None:
        """The un-landed placeholder digest never hits the engine inspect."""
        session = _make_session()
        boundary = _fake_boundary(reachable=True)
        placeholder_ref = "modulo-runner:opencode@sha256:" + "0" * 64
        real_ref = _IMAGE_REF
        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(return_value=[_ORG_ID])),
            patch(
                "modulo.db.crud.runner_probe.list_org_image_refs",
                new=AsyncMock(return_value=[placeholder_ref, real_ref]),
            ),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock()) as upsert,
        ):
            await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        boundary.image_present.assert_awaited_once_with(real_ref)
        kwargs = upsert.await_args.kwargs
        assert kwargs["images_present"] is True
        assert placeholder_ref not in kwargs["image_checks"]
        assert kwargs["image_checks"][real_ref] is True

    @pytest.mark.asyncio
    async def test_poisoned_org_infra_error_isolates_and_reraises(self) -> None:
        """qa F2: a poisoned org's infra failure ROLLS BACK ONLY ITSELF —
        org #2 still upserts in the same tick — and the tick re-raises so
        SAQ's retries engage (qa F3)."""
        session = _make_session()
        boundary = _fake_boundary(reachable=True)
        other_org = uuid.UUID("00000000-0000-0000-0000-000000000002")

        async def _list_orgs(s: Any) -> list[uuid.UUID]:
            return [_ORG_ID, other_org]

        upserted_orgs: list[uuid.UUID] = []

        async def _upsert(_session: Any, **kwargs: Any) -> None:
            upserted_orgs.append(kwargs["org_id"])
            if kwargs["org_id"] == _ORG_ID:
                raise SQLAlchemyError("statement poisoned")

        with (
            patch("modulo.db.crud.runner_probe.list_orgs_with_runner_profiles", new=AsyncMock(side_effect=_list_orgs)),
            patch("modulo.db.crud.runner_probe.list_org_image_refs", new=AsyncMock(return_value=[])),
            patch("modulo.db.crud.runner_probe.get_runner_probe_cache", new=AsyncMock(return_value=None)),
            patch("modulo.db.crud.runner_probe.prune_stale_runner_probe_rows", new=AsyncMock()),
            patch("modulo.db.crud.runner_probe.upsert_runner_probe_cache", new=AsyncMock(side_effect=_upsert)),
            pytest.raises(SQLAlchemyError),
        ):
            await run_runner_health_probe(
                _fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary
            )
        assert other_org in upserted_orgs
