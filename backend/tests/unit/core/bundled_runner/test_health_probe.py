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
    NOTIFICATION_ACTION_URL,
    NOTIFICATION_CATEGORY,
    EngineProbeOutcome,
    _emit_unreachable_transition,
    _EngineBoundary,
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


class _FakeAioDockerClient:
    """Minimal aiodocker client stand-in for the real ``_EngineBoundary``."""

    def __init__(
        self,
        info: dict[str, Any] | None = None,
        info_exc: Exception | None = None,
        inspect_result: Any = None,
        inspect_exc: Exception | None = None,
    ) -> None:
        self._info = info
        self._info_exc = info_exc
        self._inspect_result = inspect_result
        self._inspect_exc = inspect_exc
        self.closed = False

    class _System:
        def __init__(self, info: dict[str, Any] | None, info_exc: Exception | None) -> None:
            self._info = info
            self._info_exc = info_exc

        async def info(self) -> dict[str, Any]:
            if self._info_exc is not None:
                raise self._info_exc
            return self._info or {}

    @property
    def system(self) -> _System:
        return self._System(self._info, self._info_exc)

    class _Images:
        def __init__(self, result: Any, exc: Exception | None) -> None:
            self._result = result
            self._exc = exc

        async def inspect(self, ref: str) -> Any:
            if self._exc is not None:
                raise self._exc
            return self._result

    @property
    def images(self) -> _Images:
        return self._Images(self._inspect_result, self._inspect_exc)

    async def close(self) -> None:
        self.closed = True


def _make_aiodocker_client(
    info: dict[str, Any] | None = None,
    info_exc: Exception | None = None,
    inspect_result: Any = None,
    inspect_exc: Exception | None = None,
) -> _FakeAioDockerClient:
    return _FakeAioDockerClient(info=info, info_exc=info_exc, inspect_result=inspect_result, inspect_exc=inspect_exc)


def _real_boundary(client: _FakeAioDockerClient) -> _EngineBoundary:
    """A real ``_EngineBoundary`` whose ``aiodocker.Docker`` ctor is stubbed to
    return ``client`` — so the REAL engine-probe code paths run."""
    boundary = _EngineBoundary("tcp://placeholder:2375")
    boundary._client = client  # inject without touching the socket proxy
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


class TestEngineBoundaryRealClient:
    """Exercise the REAL ``_EngineBoundary`` methods (the orchestration tests
    above mock ``probe_engine``/``image_present``, so these engine-boundary
    lines are otherwise uncovered). A fake aiodocker client is injected in
    place of the real socket proxy."""

    @pytest.mark.asyncio
    async def test_probe_engine_happy_path_reads_engine_info(self) -> None:
        info = {"NCPU": 8, "MemTotal": 16384 * 1024 * 1024}
        client = _make_aiodocker_client(info=info)
        boundary = _real_boundary(client)
        outcome = await boundary.probe_engine()
        assert outcome.reachable is True
        assert outcome.engine_info == {"cpu_count": 8, "mem_total_mb": 16384}
        assert outcome.error is None

    @pytest.mark.asyncio
    async def test_probe_engine_skips_non_positive_resource_values(self) -> None:
        # Negative/zero engine values must not be recorded as engine_info.
        info = {"NCPU": 0, "MemTotal": -1}
        client = _make_aiodocker_client(info=info)
        boundary = _real_boundary(client)
        outcome = await boundary.probe_engine()
        assert outcome.reachable is True
        assert outcome.engine_info == {}

    @pytest.mark.asyncio
    async def test_probe_engine_scrubs_credentials_on_failure(self) -> None:
        exc = RuntimeError("connect failed tcp://admin:s3cret@proxy.internal:2375")
        client = _make_aiodocker_client(info_exc=exc)
        boundary = _real_boundary(client)
        outcome = await boundary.probe_engine()
        assert outcome.reachable is False
        assert "s3cret" not in outcome.error
        assert "***@" in outcome.error

    @pytest.mark.asyncio
    async def test_image_present_true_when_inspect_succeeds(self) -> None:
        client = _make_aiodocker_client(inspect_result={"Id": "abc"})
        boundary = _real_boundary(client)
        assert await boundary.image_present("ref") is True

    @pytest.mark.asyncio
    async def test_image_present_false_on_404(self) -> None:
        exc = RuntimeError("not found")
        exc.status = 404  # type: ignore[attr-defined]
        client = _make_aiodocker_client(inspect_exc=exc)
        boundary = _real_boundary(client)
        assert await boundary.image_present("ref") is False

    @pytest.mark.asyncio
    async def test_image_present_none_on_transient_inspect_failure(self) -> None:
        # A non-404 inspect failure must read UNKNOWN (qa F4), never absent.
        exc = RuntimeError("proxy hiccup")
        exc.status = 500  # type: ignore[attr-defined]
        client = _make_aiodocker_client(inspect_exc=exc)
        boundary = _real_boundary(client)
        assert await boundary.image_present("ref") is None

    @pytest.mark.asyncio
    async def test_close_releases_client(self) -> None:
        client = _make_aiodocker_client(info={})
        boundary = _real_boundary(client)
        await boundary.probe_engine()
        await boundary.close()
        assert client.closed is True


class TestEmitUnreachableTransitionReal:
    """Exercise the REAL ``_emit_unreachable_transition`` — the orchestration
    tests above mock it out, so its emit + notification paths are otherwise
    uncovered."""

    @pytest.mark.asyncio
    async def test_emits_signal_event_and_notification(self) -> None:
        session = AsyncMock()
        emit = AsyncMock()
        notify = AsyncMock()
        with (
            patch("modulo.core.error_tracking.emit_signal_event", new=emit),
            patch("modulo.db.crud.notifications.create_notification", new=notify),
        ):
            await _emit_unreachable_transition(session, _ORG_ID, "machine-1", "boom")
        emit.assert_awaited_once()
        assert emit.await_args.kwargs["signal"] == "runner_unavailable"
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["category"] == NOTIFICATION_CATEGORY
        assert notify.await_args.kwargs["action_url"] == NOTIFICATION_ACTION_URL

    @pytest.mark.asyncio
    async def test_signal_event_failure_does_not_block_notification(self) -> None:
        session = AsyncMock()
        emit = AsyncMock(side_effect=RuntimeError("event store down"))
        notify = AsyncMock()
        with (
            patch("modulo.core.error_tracking.emit_signal_event", new=emit),
            patch("modulo.db.crud.notifications.create_notification", new=notify),
        ):
            # Must not raise — both emit paths are fail-open.
            await _emit_unreachable_transition(session, _ORG_ID, "machine-1", "boom")
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notification_failure_is_swallowed(self) -> None:
        session = AsyncMock()
        emit = AsyncMock()
        notify = AsyncMock(side_effect=RuntimeError("notify store down"))
        with (
            patch("modulo.core.error_tracking.emit_signal_event", new=emit),
            patch("modulo.db.crud.notifications.create_notification", new=notify),
        ):
            await _emit_unreachable_transition(session, _ORG_ID, "machine-1", "boom")
        emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_poisoned_org_infra_error_isolates_and_reraises() -> None:
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
        await run_runner_health_probe(_fake_session_factory(session), machine_id="machine-1", engine_boundary=boundary)
    assert other_org in upserted_orgs
