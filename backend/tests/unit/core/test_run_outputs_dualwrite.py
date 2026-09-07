"""Unit tests for the FAR-583 run-outputs dual-write orchestration.

Covers the core-side surface (:mod:`modulo.core.run_outputs_dualwrite`) and the
extended ``saq_hooks._mark_run_failed`` primitive:

* the kill-switch read — per-call semantics (a flip between calls changes the
  behaviour) and the fail-closed default when the store read fails;
* ``orchestrate_dual_write_failure`` — the separate-session terminalize with
  ``error_code='dual_write_failed'`` + bounded lock_timeout, the best-effort
  error event, and the best-effort Redis counters;
* ``guard_dual_write`` — rollback-BEFORE-orchestrate ordering, then re-raise;
* ``_mark_run_failed``'s guard extension — the FULL terminal vocabulary plus
  the explicit ``'unknown'`` exclusion (an unknown run is never transitioned),
  driven against a real SQLite engine so the raw SQL actually executes.

DB-backed cases run on in-memory SQLite with ``Base.metadata.create_all`` over
the involved tables only (no migrations): the raw UPDATE's Postgres-style
``now()`` is exposed via ``create_function`` (the
``test_run_classification`` precedent).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.core import run_outputs_dualwrite as dualwrite_module
from modulo.core.error_tracking import saq_hooks
from modulo.core.run_outputs_dualwrite import (
    DUAL_WRITE_COUNTERS,
    DUAL_WRITE_ENABLED_KEY,
    _redis_incr_window,
    _redis_set_nx,
    bump_dual_write_counter,
    guard_dual_write,
    is_dual_write_enabled,
    note_dual_write_disabled,
    orchestrate_dual_write_failure,
    read_dual_write_counters,
    set_dual_write_enabled,
)
from modulo.core.runtime_config.store import get_runtime_config_store
from modulo.db.crud.run_node_outputs import DualWriteError
from modulo.db.models.base import Base
from modulo.db.models.organisation import Organisation
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.models.run_node_outputs import RunNodeOutput

_ORG = uuid.uuid4()
_PIPELINE = uuid.uuid4()
_SNAPSHOT = uuid.uuid4()

_RUN_AND_ORG_TABLES = (Organisation.__table__, Run.__table__, RunNodeOutput.__table__)


def _dual_write_error(**overrides: Any) -> DualWriteError:
    run_id = overrides.pop("run_id", uuid.uuid4())
    org_id = overrides.pop("organisation_id", _ORG)
    return DualWriteError(
        "injected dual-write failure",
        run_id=run_id,
        organisation_id=org_id,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


class _FakeRedis:
    """A scripted async Redis double for the switch/counter helpers."""

    def __init__(self, *, get_value: Any = None, get_error: Exception | None = None) -> None:
        self.get_value = get_value
        self.get_error = get_error
        self.set_calls: list[dict[str, Any]] = []
        self.expire_calls: list[dict[str, Any]] = []
        self.get_calls: list[Any] = []
        self.closed = False

    async def get(self, key: Any) -> Any:
        self.get_calls.append(key)
        if self.get_error is not None:
            raise self.get_error
        return self.get_value

    async def set(self, key: Any, value: Any, **kwargs: Any) -> Any:
        self.set_calls.append({"key": key, "value": value, **kwargs})
        return True

    async def expire(self, key: Any, ttl: Any, **kwargs: Any) -> Any:
        self.expire_calls.append({"key": key, "ttl": ttl, **kwargs})
        return True

    async def incr(self, key: Any) -> int:
        self.set_calls.append({"key": key, "value": "__incr__"})
        return 1

    async def incrby(self, key: Any, amount: int) -> int:
        self.set_calls.append({"key": key, "value": f"__incrby__{amount}"})
        return amount

    async def aclose(self) -> None:
        self.closed = True


def _redis_client(**kwargs: Any) -> _FakeRedis:
    return _FakeRedis(**kwargs)


@pytest.fixture(autouse=True)
def _fresh_switch_cache() -> Generator[None, None, None]:
    """qa Minor 5: reset the ~1s switch-read TTL cache around EVERY test —
    the module-level cache would otherwise leak an OFF (or ON) resolution
    across tests and make switch-dependent suites order-flaky."""
    dualwrite_module._reset_switch_read_cache()
    yield
    dualwrite_module._reset_switch_read_cache()


class TestKillSwitch:
    """qa C3: the switch is FLEET-VISIBLE — the Redis key is read first, the
    process-local runtime-config override is the fallback leg, default ON."""

    def setup_method(self) -> None:
        get_runtime_config_store().clear_all_overrides()
        import modulo.core.run_outputs_dualwrite as module

        module._BOOT_OFF_WARNING_EMITTED = False

    def teardown_method(self) -> None:
        get_runtime_config_store().clear_all_overrides()

    @pytest.mark.asyncio
    async def test_default_is_on(self) -> None:
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_redis_key_off_disables_fleet_wide(self) -> None:
        """The fleet path: the SAQ worker machines never see an admin API
        flip, but they DO see the Redis key — '0' disables dual-write."""
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="0")):
            assert await is_dual_write_enabled() is False

    @pytest.mark.asyncio
    async def test_redis_key_false_disables_and_present_value_enables(self) -> None:
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="false")):
            assert await is_dual_write_enabled() is False
        dualwrite_module._reset_switch_read_cache()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="1")):
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_redis_error_falls_through_to_override_then_default(self) -> None:
        """A Redis outage falls through: the override still governs the local
        process, and with no override the fail-closed default keeps ON."""
        with (
            patch(
                "modulo.core.run_outputs_dualwrite._open_redis",
                return_value=_redis_client(get_error=RuntimeError("down")),
            ),
            patch("modulo.core.runtime_config.store.get_runtime_config_store") as store_factory,
        ):
            store = store_factory.return_value
            store.get.return_value = "false"
            assert await is_dual_write_enabled() is False
            dualwrite_module._reset_switch_read_cache()
            store.get.return_value = None
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_redis_absent_key_honors_override(self) -> None:
        """The process-local leg stays honoured when the Redis key is absent —
        the web-process/tests flip path keeps working."""
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, "false")
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
            assert await is_dual_write_enabled() is False
        get_runtime_config_store().clear_override(DUAL_WRITE_ENABLED_KEY)
        dualwrite_module._reset_switch_read_cache()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_redis_precedence_beats_override(self) -> None:
        """The fleet key wins: an OFF Redis key disables even where the local
        override says ON."""
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, "true")
        try:
            with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="0")):
                assert await is_dual_write_enabled() is False
        finally:
            get_runtime_config_store().clear_override(DUAL_WRITE_ENABLED_KEY)

    @pytest.mark.asyncio
    async def test_override_leg_literal_false_only_disables(self) -> None:
        """On the process-local leg only the literal "false" (any case)
        disables — anything else, including "off", keeps dual-write ON."""
        store = get_runtime_config_store()
        store.set_override(DUAL_WRITE_ENABLED_KEY, " False ")
        try:
            with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
                assert await is_dual_write_enabled() is False
        finally:
            store.clear_override(DUAL_WRITE_ENABLED_KEY)
        dualwrite_module._reset_switch_read_cache()
        store.set_override(DUAL_WRITE_ENABLED_KEY, "off")
        try:
            with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
                assert await is_dual_write_enabled() is True
        finally:
            store.clear_override(DUAL_WRITE_ENABLED_KEY)

    @pytest.mark.asyncio
    async def test_store_read_failure_is_fail_closed_on(self) -> None:
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)),
            patch("modulo.core.runtime_config.store.get_runtime_config_store") as store_factory,
        ):
            store = store_factory.return_value
            store.get.side_effect = RuntimeError("store unavailable")
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_boot_warning_emitted_once_on_first_off_read(self, caplog: pytest.LogCaptureFixture) -> None:
        """The FIRST process-local OFF read logs the loud degraded-combo
        warning (readers serve the new table, writes legacy-only, sweep
        heals); later OFF reads stay silent."""
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        get_runtime_config_store().set_override(DUAL_WRITE_ENABLED_KEY, "false")
        try:
            with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
                assert await is_dual_write_enabled() is False
                dualwrite_module._reset_switch_read_cache()
                assert await is_dual_write_enabled() is False
        finally:
            get_runtime_config_store().clear_override(DUAL_WRITE_ENABLED_KEY)
        warnings = [r for r in caplog.records if "dual_write_switch_off_degraded_combo" in r.message]
        assert len(warnings) == 1
        assert "LEGACY-ONLY" in warnings[0].message
        assert "sweep" in warnings[0].message

    @pytest.mark.asyncio
    async def test_boot_warning_fires_for_redis_off_too(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="0")):
            assert await is_dual_write_enabled() is False
        assert any("dual_write_switch_off_degraded_combo" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_set_dual_write_enabled_writes_fleet_key(self, caplog: pytest.LogCaptureFixture) -> None:
        """The ops runbook flip: SET the fleet key (no NX — an explicit
        re-issue overwrites), bounded by the TTL; the call is loud."""
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        client = _redis_client()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            await set_dual_write_enabled(False, ttl_seconds=1800)
            await set_dual_write_enabled(True, ttl_seconds=60)
        assert client.set_calls[0] == {"key": "saq:run_outputs:dual_write_enabled", "value": "0", "ex": 1800}
        assert client.set_calls[1] == {"key": "saq:run_outputs:dual_write_enabled", "value": "1", "ex": 60}
        assert any("dual_write_switch_flipped" in r.message for r in caplog.records)


class TestSwitchOutageLatch:
    """qa iteration 2 (Major 3): a Redis read EXCEPTION serves the last-seen
    latch — an operator's emergency OFF cannot be silently un-flipped by a
    Redis blip; default ON only when no explicit value has ever been read;
    an explicit key REMOVAL clears the latch."""

    @pytest.mark.asyncio
    async def test_off_survives_a_redis_outage(self) -> None:
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="0")):
            assert await is_dual_write_enabled() is False
        # Expire the cache WITHOUT clearing the latch (the reset helper drops
        # both): backdate the cached read past the TTL.
        cached = dualwrite_module._SWITCH_CACHE
        dualwrite_module._SWITCH_CACHE = (
            cached[0] - dualwrite_module._SWITCH_CACHE_TTL_SECONDS - 1.0,
            cached[1],
        )
        assert dualwrite_module._LAST_SEEN_REDIS_STATE is False
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            return_value=_redis_client(get_error=RuntimeError("redis down")),
        ):
            assert await is_dual_write_enabled() is False, "the emergency OFF must survive the blip"

    @pytest.mark.asyncio
    async def test_never_set_redis_down_stays_fail_closed_on(self) -> None:
        assert dualwrite_module._LAST_SEEN_REDIS_STATE is None
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            return_value=_redis_client(get_error=RuntimeError("redis down")),
        ):
            assert await is_dual_write_enabled() is True, "no explicit value ever read → fail-closed ON"

    @pytest.mark.asyncio
    async def test_explicit_removal_clears_the_latch(self) -> None:
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="0")):
            assert await is_dual_write_enabled() is False
        # Redis recovers, key REMOVED (get → None): falls through to ON.
        dualwrite_module._reset_switch_read_cache()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value=None)):
            assert await is_dual_write_enabled() is True, "explicit removal honored"
        assert dualwrite_module._LAST_SEEN_REDIS_STATE is None
        # And a SUBSEQUENT outage falls through too — the stale OFF is gone.
        dualwrite_module._reset_switch_read_cache()
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            return_value=_redis_client(get_error=RuntimeError("redis down again")),
        ):
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_explicit_flip_latches_the_value(self) -> None:
        """set_dual_write_enabled latches the explicit value — an immediate
        Redis blip after the flip must not un-flip it."""
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client()):
            await set_dual_write_enabled(False, ttl_seconds=60)
        assert dualwrite_module._LAST_SEEN_REDIS_STATE is False
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            return_value=_redis_client(get_error=RuntimeError("blip")),
        ):
            assert await is_dual_write_enabled() is False

    @pytest.mark.asyncio
    async def test_latch_survives_cache_expiry(self) -> None:
        """The latch is NOT the cache: expiry (or a reset) drops the cached
        value but the last-seen explicit state still governs an outage."""
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=_redis_client(get_value="0")):
            assert await is_dual_write_enabled() is False
        cached = dualwrite_module._SWITCH_CACHE
        dualwrite_module._SWITCH_CACHE = (cached[0] - dualwrite_module._SWITCH_CACHE_TTL_SECONDS - 1.0, cached[1])
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            return_value=_redis_client(get_error=RuntimeError("down")),
        ):
            assert await is_dual_write_enabled() is False

    @pytest.mark.asyncio
    async def test_switch_read_is_single_flight(self) -> None:
        """qa iteration 2 (rider 9): a concurrent cache miss awaits the ONE
        refresh — the second caller does not open its own Redis client."""
        release = asyncio.Event()
        started = asyncio.Event()
        factory_calls: list[int] = []

        class _SlowRedis:
            async def get(self, key: Any) -> Any:
                started.set()
                await release.wait()
                return b"0"

            async def aclose(self) -> None:
                pass

        def _factory() -> Any:
            factory_calls.append(1)
            return _SlowRedis()

        async def _reader() -> bool:
            return await is_dual_write_enabled()

        with patch("modulo.core.run_outputs_dualwrite._open_redis", _factory):
            task = asyncio.create_task(_reader())
            await started.wait()
            concurrent = asyncio.create_task(_reader())
            await asyncio.sleep(0)  # let the concurrent task reach the in-lock wait
            release.set()
            assert await task is False
            assert await concurrent is False
        assert len(factory_calls) == 1, "single-flight: one refresh, one client"
        dualwrite_module._reset_switch_read_cache()


class TestSwitchReadCache:
    """qa Minor 5 + iteration 2 rider 9: the switch read is served through a
    ~5s in-process TTL cache (above the 2s socket timeout) with a
    single-flight refresh — one Redis read per window instead of a fresh
    client + GET per chokepoint call (the marker write holds the run row FOR
    UPDATE, so a hung Redis must not stall it per call)."""

    @pytest.mark.asyncio
    async def test_two_calls_within_ttl_read_redis_once(self) -> None:
        factory = MagicMock(return_value=_redis_client(get_value="1"))
        with patch("modulo.core.run_outputs_dualwrite._open_redis", factory):
            assert await is_dual_write_enabled() is True
            assert await is_dual_write_enabled() is True
        assert factory.call_count == 1, "the second call inside the TTL must be served from the cache"

    @pytest.mark.asyncio
    async def test_off_value_is_cached_within_ttl(self) -> None:
        factory = MagicMock(return_value=_redis_client(get_value="0"))
        with patch("modulo.core.run_outputs_dualwrite._open_redis", factory):
            assert await is_dual_write_enabled() is False
            assert await is_dual_write_enabled() is False
        assert factory.call_count == 1

    @pytest.mark.asyncio
    async def test_ttl_expiry_rereads_and_serves_the_new_value(self) -> None:
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            MagicMock(return_value=_redis_client(get_value="0")),
        ):
            assert await is_dual_write_enabled() is False
        cached = dualwrite_module._SWITCH_CACHE
        assert cached is not None
        # Backdate the cached read past the TTL (no wall-clock sleeps).
        dualwrite_module._SWITCH_CACHE = (
            cached[0] - dualwrite_module._SWITCH_CACHE_TTL_SECONDS - 1.0,
            cached[1],
        )
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            MagicMock(return_value=_redis_client(get_value="1")),
        ):
            assert await is_dual_write_enabled() is True

    @pytest.mark.asyncio
    async def test_reset_helper_forces_a_reread(self) -> None:
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            MagicMock(return_value=_redis_client(get_value="1")),
        ):
            assert await is_dual_write_enabled() is True
        dualwrite_module._reset_switch_read_cache()
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            MagicMock(return_value=_redis_client(get_value="0")),
        ):
            assert await is_dual_write_enabled() is False

    @pytest.mark.asyncio
    async def test_set_dual_write_enabled_invalidates_the_cache(self) -> None:
        """The flip must be observable immediately in the flipping process —
        without the invalidation the ~1s cache would serve the pre-flip value."""
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            MagicMock(return_value=_redis_client(get_value="1")),
        ):
            assert await is_dual_write_enabled() is True
        with patch(
            "modulo.core.run_outputs_dualwrite._open_redis",
            MagicMock(return_value=_redis_client(get_value="0")),
        ):
            await set_dual_write_enabled(False, ttl_seconds=60)
            assert await is_dual_write_enabled() is False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_terminalize_uses_error_code_and_lock_timeout(self) -> None:
        exc = _dual_write_error(claim_token="tok-1", sqlstate="42501", origin="update_run_status.orm")
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=1) as mark,
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 1
        mark.assert_awaited_once()
        kwargs = mark.await_args.kwargs
        assert kwargs["error_code"] == "dual_write_failed"
        assert kwargs["claim_token"] == "tok-1"
        assert kwargs["lock_timeout_ms"] is not None
        assert mark.await_args.args == (str(exc.run_id), str(_ORG))

    @pytest.mark.asyncio
    async def test_rowcount_zero_superseded_still_returns_zero(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=0) as mark,
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 0
        mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_event_failure_is_swallowed(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=1),
            patch(
                "modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ingest down"),
            ),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock),
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 1

    @pytest.mark.asyncio
    async def test_terminalize_failure_is_swallowed_and_counters_still_fire(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(
                saq_hooks,
                "_mark_run_failed",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
            patch(
                "modulo.core.run_outputs_dualwrite.bump_dual_write_counter",
                new_callable=AsyncMock,
            ) as bump,
        ):
            rowcount = await orchestrate_dual_write_failure(exc)
        assert rowcount == 0
        fields = {call.args[0] for call in bump.await_args_list}
        assert fields == {"outputs_dual_write_failed"}

    @pytest.mark.asyncio
    async def test_guard_rolls_back_before_orchestrating_then_reraises(self) -> None:
        session = AsyncMock()
        exc = _dual_write_error()
        order: list[str] = []

        async def _rollback() -> None:
            order.append("rollback")

        async def _orchestrate(*args: Any, **kwargs: Any) -> int:
            order.append("orchestrate")
            return 1

        session.rollback = _rollback
        with (
            patch(
                "modulo.core.run_outputs_dualwrite.orchestrate_dual_write_failure",
                _orchestrate,
            ),
            pytest.raises(DualWriteError) as caught,
        ):
            async with guard_dual_write(session):
                raise exc
        assert caught.value is exc
        assert order[0] == "rollback"
        assert order[1] == "orchestrate"


# ---------------------------------------------------------------------------
# _mark_run_failed guard extension (real SQLite engine)
# ---------------------------------------------------------------------------


async def _now_sqlite(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_now(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.create_function("now", 0, lambda: datetime.now(UTC).isoformat())


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    await _now_sqlite(eng)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_RUN_AND_ORG_TABLES))
        # Production _mark_run_failed never hits FK-adjacent tables; the runs
        # FK to organisations/pipelines would fail create-order inserts, so the
        # reference is left unenforced exactly like the
        # test_run_classification harness.
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
def sqlite_sessionmaker(sqlite_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)


async def _seed_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    status: str = "running",
    claim_token: str | None = "tok-a",
    organisation_id: uuid.UUID = _ORG,
) -> None:
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO organisations (id, name, slug, settings_json, otel_config_json) "
                "VALUES (:id, 'mark-run-failed org', :slug, '{}', '{}')"
            ),
            {"id": str(organisation_id), "slug": f"mark-run-failed-{organisation_id.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, "
                "run_number, input_hash, langgraph_thread_id, claim_token, cancellation_requested) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :status, 1, 'ih', :thread, :tok, 0)"
            ),
            {
                "id": run_id.hex,
                "oid": organisation_id.hex,
                "pid": _PIPELINE.hex,
                "sid": _SNAPSHOT.hex,
                "status": status,
                "thread": f"mark-run-failed-{run_id}",
                "tok": claim_token,
            },
        )


async def _read_run(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> dict[str, Any]:
    async with maker() as session, session.begin():
        row = (
            await session.execute(
                text("SELECT status, error_code, claim_token FROM runs WHERE id = :rid"),
                {"rid": run_id.hex},
            )
        ).fetchone()
    assert row is not None
    return {"status": row[0], "error_code": row[1], "claim_token": row[2]}


class TestMarkRunFailedGuard:
    @pytest.mark.asyncio
    async def test_running_run_is_terminalized_with_custom_error_code(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(
                run_id.hex,
                _ORG.hex,
                claim_token="tok-a",
                error_code="dual_write_failed",
                error_detail="dual-write leg failed",
            )
        assert rowcount == 1
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["status"] == "failed"
        assert row["error_code"] == "dual_write_failed"

    @pytest.mark.asyncio
    async def test_default_error_code_is_task_failure(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id)
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(run_id.hex, _ORG.hex)
        assert rowcount == 1
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["error_code"] == "task_failure"

    @pytest.mark.asyncio
    async def test_unknown_run_is_never_transitioned(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id, status="unknown")
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(
                run_id.hex,
                _ORG.hex,
                error_code="dual_write_failed",
            )
        assert rowcount == 0
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_claim_token_fence_honored(self, sqlite_sessionmaker: Any) -> None:
        run_id = uuid.uuid4()
        await _seed_run(sqlite_sessionmaker, run_id, claim_token="tok-current")
        with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
            rowcount = await saq_hooks._mark_run_failed(
                run_id.hex,
                _ORG.hex,
                claim_token="tok-stale",
                error_code="dual_write_failed",
            )
        assert rowcount == 0
        row = await _read_run(sqlite_sessionmaker, run_id)
        assert row["status"] == "running"
        assert row["claim_token"] == "tok-current"

    @pytest.mark.asyncio
    async def test_full_terminal_vocabulary_is_excluded(self, sqlite_sessionmaker: Any) -> None:
        # Every TERMINAL_STATUSES member must be excluded by the guard — drive
        # the real primitive against each status and require rowcount 0.
        for status in sorted(TERMINAL_STATUSES):
            run_id = uuid.uuid4()
            org_id = uuid.uuid4()
            await _seed_run(sqlite_sessionmaker, run_id, status=status, organisation_id=org_id)
            with patch.object(saq_hooks, "_open_factory", return_value=sqlite_sessionmaker):
                rowcount = await saq_hooks._mark_run_failed(
                    run_id.hex,
                    org_id.hex,
                    error_code="dual_write_failed",
                )
            assert rowcount == 0
            row = await _read_run(sqlite_sessionmaker, run_id)
            assert row["status"] == status

    @pytest.mark.asyncio
    async def test_invalid_error_code_marker_is_rejected(self, sqlite_sessionmaker: Any) -> None:
        with pytest.raises(ValueError, match="invalid error_code marker"):
            await saq_hooks._mark_run_failed(
                uuid.uuid4().hex,
                _ORG.hex,
                error_code="bad'; DROP TABLE runs; --",
            )

    @pytest.mark.asyncio
    async def test_lock_timeout_is_skipped_on_sqlite(self, sqlite_sessionmaker: Any) -> None:
        # The SET LOCAL is Postgres-only; on SQLite it must be skipped without
        # touching the mock session's execute count.
        session = AsyncMock()
        result = AsyncMock()
        result.rowcount = 0
        session.execute.return_value = result
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        with (
            patch.object(saq_hooks, "_open_factory", return_value=MagicMock(return_value=session)),
            patch("modulo.db.rls.set_rls_org", new_callable=AsyncMock),
        ):
            rowcount = await saq_hooks._mark_run_failed(
                uuid.uuid4().hex,
                _ORG.hex,
                error_code="dual_write_failed",
                lock_timeout_ms=2000,
            )
        assert rowcount == 0
        assert session.execute.await_count == 1


# ---------------------------------------------------------------------------
# The dual-write chokepoint helper (db side) — SQLite, no RLS-org sessions
# ---------------------------------------------------------------------------


class TestDualWriteHelperSkipsWithoutRlsOrg:
    @pytest.mark.asyncio
    async def test_no_rls_org_skips_the_new_table_leg(self, sqlite_sessionmaker: Any) -> None:
        """A session with no bound RLS org skips the new-table leg silently.

        The repo's write gate requires a bound org; the only org-less sessions
        are unit tests / maintenance sessions where the legacy write governs.
        The absence of a raised error IS the assertion (a real replace attempt
        would either raise the org gate or hit the table).
        """
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            await dual_write_run_node_outputs(
                session,
                run_id=run_id,
                organisation_id=_ORG,
                outputs={"n1": {"a": 1}},
                telemetry=None,
            )
        row = await _read_run(maker, run_id)
        assert row["status"] == "running"


class TestDualWriteHelperKillSwitchOff:
    @pytest.mark.asyncio
    async def test_kill_switch_off_skips_with_degraded_note(self, sqlite_sessionmaker: Any) -> None:
        from modulo.core.run_outputs_dualwrite import note_dual_write_disabled
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        store = get_runtime_config_store()
        store.set_override(DUAL_WRITE_ENABLED_KEY, "false")
        try:
            async with maker() as session, session.begin():
                await set_rls_org_for_test(session, _ORG)
                with patch(
                    "modulo.core.run_outputs_dualwrite.note_dual_write_disabled",
                    wraps=note_dual_write_disabled,
                ) as note:
                    await dual_write_run_node_outputs(
                        session,
                        run_id=run_id,
                        organisation_id=_ORG,
                        outputs={"n1": {"a": 1}},
                        telemetry=None,
                    )
            assert note.await_count == 1
        finally:
            store.clear_override(DUAL_WRITE_ENABLED_KEY)


async def set_rls_org_for_test(session: AsyncSession, org_id: uuid.UUID) -> None:
    """SQLite-side RLS binding (session.info), bypassing the transaction guard."""
    from modulo.db.rls import _TENANT_KEY

    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info[_TENANT_KEY] = org_id


class TestDualWriteHelperSentinelAbort:
    @pytest.mark.asyncio
    async def test_sentinel_squatting_payload_fails_closed(self, sqlite_sessionmaker: Any) -> None:
        """A ``__``-prefixed node id in the payload aborts the dual-write leg.

        The repo's sentinel gate raises inside the savepoint; the helper
        converts it to the fail-closed DualWriteError (non-retryable).
        """
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with pytest.raises(DualWriteError) as caught:
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"__run_meta__": {"squat": True}},
                    telemetry=None,
                    origin="unit-test",
                )
        assert caught.value.origin == "unit-test"
        assert caught.value.sqlstate is None


class TestDualWriteHelperRetryable:
    @pytest.mark.asyncio
    async def test_statement_timeout_57014_retries_once_then_succeeds(self, sqlite_sessionmaker: Any) -> None:
        """A 57014 (statement timeout) failure once → the retry leg runs the
        REAL write. ONLY 57014 stays retryable — the transaction-aborting
        states abort the whole transaction on Postgres, so re-entering a
        savepoint after them would fail with 25P02 (qa iteration-1)."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        real_replace = run_crud.replace_run_node_outputs
        calls: list[int] = []

        async def _flaky_replace(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                err = OperationalError("stmt", {}, Exception("statement timeout"))
                err.orig = type("_FakePG", (Exception,), {"sqlstate": "57014"})("statement timeout")
                raise err
            return await real_replace(*args, **kwargs)

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _flaky_replace),
                patch(
                    "modulo.core.run_outputs_dualwrite.bump_dual_write_counter",
                    new_callable=AsyncMock,
                ),
            ):
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_serialization_failure_40001_raises_immediately(self, sqlite_sessionmaker: Any) -> None:
        """40001 aborts the WHOLE Postgres transaction — a savepoint retry
        after it would fail with 25P02 (doomed round-trip), so the helper goes
        STRAIGHT to DualWriteError (ONE replace attempt, no retry leg)."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        calls: list[int] = []

        async def _failing(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            err = OperationalError("stmt", {}, Exception("could not serialize"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "40001"})("could not serialize")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _failing),
                patch(
                    "modulo.core.run_outputs_dualwrite.bump_dual_write_counter",
                    new_callable=AsyncMock,
                ) as bump,
                pytest.raises(DualWriteError) as caught,
            ):
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert calls == [1], "40001 must NOT be retried in-session"
        assert caught.value.sqlstate == "40001"
        fields = {call.args[0] for call in bump.await_args_list}
        assert "outputs_dual_write_retries" not in fields

    @pytest.mark.asyncio
    async def test_retryable_sqlstate_twice_raises_dual_write_error(self, sqlite_sessionmaker: Any) -> None:
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        async def _always_failing(*args: Any, **kwargs: Any) -> None:
            err = OperationalError("stmt", {}, Exception("statement timeout"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "57014"})("statement timeout")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _always_failing),
                patch(
                    "modulo.core.run_outputs_dualwrite.bump_dual_write_counter",
                    new_callable=AsyncMock,
                ),
                pytest.raises(DualWriteError) as caught,
            ):
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert caught.value.sqlstate == "57014"

    @pytest.mark.asyncio
    async def test_deadlock_40p01_raises_immediately(self, sqlite_sessionmaker: Any) -> None:
        """40P01 (deadlock) aborts the transaction → straight to
        DualWriteError, no doomed savepoint retry."""
        from sqlalchemy.exc import OperationalError

        from modulo.db.crud import run as run_crud
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)

        calls: list[int] = []

        async def _always_failing(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            err = OperationalError("stmt", {}, Exception("deadlock"))
            err.orig = type("_FakePG", (Exception,), {"sqlstate": "40P01"})("deadlock")
            raise err

        async with maker() as session, session.begin():
            await set_rls_org_for_test(session, _ORG)
            with (
                patch.object(run_crud, "replace_run_node_outputs", _always_failing),
                patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock),
                pytest.raises(DualWriteError) as caught,
            ):
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        assert calls == [1]
        assert caught.value.sqlstate == "40P01"


# ---------------------------------------------------------------------------
# qa iteration-1 riders: dedicated counters, Redis races, degraded signal
# ---------------------------------------------------------------------------


class TestRedisHelpers:
    """qa M12/M13 + riders (a): bounded Redis, TTL-at-creation, loud failures."""

    def test_socket_timeout_is_bounded(self) -> None:
        """qa M13: _open_redis bounds BOTH socket timeouts — a hung Redis
        (accept but never answer) must not stall the abort path forever."""
        import redis.asyncio as redis_module

        from modulo.core.run_outputs_dualwrite import _open_redis

        created: dict[str, Any] = {}

        def _from_url(url: str, **kwargs: Any) -> Any:
            created.update(kwargs)
            return MagicMock()

        with (
            patch.object(redis_module, "Redis") as redis_cls,
            patch("modulo.settings.get_settings", return_value=MagicMock(redis_url="redis://localhost:6379/0")),
        ):
            redis_cls.from_url = _from_url
            _open_redis()
        assert created["socket_connect_timeout"] == 2
        assert created["socket_timeout"] == 2

    @pytest.mark.asyncio
    async def test_incr_window_stamps_ttl_at_creation(self) -> None:
        """qa M12: the window TTL is stamped by a SET NX EX BEFORE the INCR —
        a process death between the two leaves a key that still expires, so
        the dual_write_failed event channel can never be suppressed forever."""
        client = _FakeRedis()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            count = await _redis_incr_window("saq:run_outputs:dual_write_failed_window:o1", 60)
        assert count == 1
        assert client.set_calls[0] == {
            "key": "saq:run_outputs:dual_write_failed_window:o1",
            "value": 0,
            "ex": 60,
            "nx": True,
        }
        assert client.set_calls[1] == {"key": "saq:run_outputs:dual_write_failed_window:o1", "value": "__incrby__1"}
        # Fixed window: the post-increment heal is NX (restores a no-TTL key
        # WITHOUT deferring the window).
        assert client.expire_calls[-1] == {
            "key": "saq:run_outputs:dual_write_failed_window:o1",
            "ttl": 60,
            "nx": True,
        }

    @pytest.mark.asyncio
    async def test_expiry_in_gap_heals_the_no_ttl_key(self) -> None:
        """qa iteration 2 (Major 6, second form): if the key expires between
        the SET NX EX and the INCRBY, INCRBY recreates it with NO TTL — the
        post-increment EXPIRE NX restores one so the window can never
        suppress events forever. Simulated: the SET lands, the key
        'expires', INCRBY recreates bare, and the EXPIRE still fires."""
        client = _FakeRedis()

        async def _incr_recreates_bare(key: Any, amount: int) -> int:
            # The key expired in the gap: INCRBY recreates it with NO TTL.
            return amount

        client.incrby = _incr_recreates_bare  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            count = await _redis_incr_window("k", 60)
        assert count == 1
        assert client.expire_calls == [{"key": "k", "ttl": 60, "nx": True}], (
            "the NX expiry-in-gap heal must run after every increment"
        )

    @pytest.mark.asyncio
    async def test_counter_ttl_is_rolling(self) -> None:
        """qa iteration 2 (Major 6): the counter's 7-day TTL re-stamps on
        EVERY bump (last-bump + 7d, a NON-NX EXPIRE) — an actively-failing
        counter never silently resets mid-incident."""
        client = _FakeRedis()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            await bump_dual_write_counter("outputs_dual_write_failed", delta=3)
        assert client.set_calls[0] == {
            "key": "saq:run_outputs:counters:outputs_dual_write_failed",
            "value": 0,
            "ex": 7 * 24 * 3600,
            "nx": True,
        }
        assert client.set_calls[1] == {
            "key": "saq:run_outputs:counters:outputs_dual_write_failed",
            "value": "__incrby__3",
        }
        assert client.expire_calls == [
            {"key": "saq:run_outputs:counters:outputs_dual_write_failed", "ttl": 7 * 24 * 3600, "nx": False}
        ], "rolling window: the post-increment EXPIRE is NON-NX (re-stamps every bump)"

    @pytest.mark.asyncio
    async def test_incr_window_failure_between_set_and_incr_still_expires(self) -> None:
        """Injected failure AFTER the SET NX EX but before/during the INCRBY:
        the TTL is already stamped (the SET landed) — the key still expires."""
        client = _FakeRedis()

        async def _boom(key: Any, amount: int) -> int:
            raise RuntimeError("process dies here")

        client.incrby = _boom  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            assert await _redis_incr_window("k", 60) is None
        assert client.set_calls[0]["ex"] == 60, "the TTL survived the injected failure"

    @pytest.mark.asyncio
    async def test_set_nx_failure_returns_none_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """qa rider (a): a Redis failure is LOGGED (never silent None)."""
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        client = _FakeRedis()

        async def _boom(key: Any, value: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        client.set = _boom  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            assert await _redis_set_nx("k", 60) is None
        assert any("redis_set_nx_failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_incr_window_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        client = _FakeRedis()

        async def _boom(key: Any, amount: int) -> int:
            raise RuntimeError("redis down")

        client.incrby = _boom  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            assert await _redis_incr_window("k", 60) is None
        assert any("redis_incr_window_failed" in r.message for r in caplog.records)


class TestDedicatedCounters:
    """qa M10: the dual-write counters live on DEDICATED cumulative keys the
    reconcile tick's wholesale blob-rewrite never touches."""

    @pytest.mark.asyncio
    async def test_bump_targets_dedicated_key_with_ttl_at_creation(self) -> None:
        client = _FakeRedis()
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            await bump_dual_write_counter("outputs_dual_write_failed", delta=3)
        prefix = "saq:run_outputs:counters:"
        assert client.set_calls[0] == {
            "key": f"{prefix}outputs_dual_write_failed",
            "value": 0,
            "ex": 7 * 24 * 3600,
            "nx": True,
        }
        assert client.set_calls[1] == {"key": f"{prefix}outputs_dual_write_failed", "value": "__incrby__3"}

    @pytest.mark.asyncio
    async def test_unknown_counter_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown dual-write counter"):
            await bump_dual_write_counter("not_a_counter")

    @pytest.mark.asyncio
    async def test_bump_failure_is_swallowed_with_a_log(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        client = _FakeRedis()

        async def _boom(key: Any, value: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        client.set = _boom  # type: ignore[method-assign]
        with patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client):
            await bump_dual_write_counter("outputs_dual_write_failed")  # must not raise
        assert any("dual_write_counter_bump_failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_read_counters_maps_missing_keys_to_zero(self) -> None:
        client = _FakeRedis()

        async def _mget(keys: list[str]) -> list[Any]:
            assert keys == [f"saq:run_outputs:counters:{name}" for name in DUAL_WRITE_COUNTERS]
            return [b"7", None, b"2", None, b"0"]

        client.mget = _mget  # type: ignore[method-assign]
        counters = await read_dual_write_counters(client)
        assert list(counters) == list(DUAL_WRITE_COUNTERS)
        assert counters["outputs_dual_write_failed"] == 7
        assert counters["outputs_dual_write_retries"] == 0
        assert counters["outputs_dual_write_degraded"] == 2

    @pytest.mark.asyncio
    async def test_orchestration_bumps_dedicated_counter(self) -> None:
        exc = _dual_write_error()
        with (
            patch.object(saq_hooks, "_mark_run_failed", new_callable=AsyncMock, return_value=1),
            patch("modulo.core.run_outputs_dualwrite._emit_dual_write_failed_event", new_callable=AsyncMock),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock) as bump,
        ):
            await orchestrate_dual_write_failure(exc)
        assert bump.await_args.args == ("outputs_dual_write_failed",)


class TestNoteDualWriteDisabled:
    """qa riders (b)/(c) + iteration 2 rider 10: per-org edge key, the
    process-local noted-window map, and the token-bucketed Redis-down
    fallback."""

    @pytest.mark.asyncio
    async def test_edge_key_is_per_org(self) -> None:
        client = _FakeRedis()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", new_callable=AsyncMock),
        ):
            await note_dual_write_disabled("run-1", _ORG)
        assert not client.get_calls  # set_nx, not get
        assert client.set_calls[0]["key"] == f"saq:run_outputs:dual_write:degraded_edge:{_ORG}"
        assert client.set_calls[0]["nx"] is True
        assert client.set_calls[0]["ex"] == 3600

    @pytest.mark.asyncio
    async def test_org_less_edge_key_has_no_suffix(self) -> None:
        client = _FakeRedis()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", new_callable=AsyncMock),
        ):
            await note_dual_write_disabled("run-1", None)
        assert client.set_calls[0]["key"] == "saq:run_outputs:dual_write:degraded_edge"

    @pytest.mark.asyncio
    async def test_redis_down_emits_token_bucketed_not_per_write(self) -> None:
        """qa iteration 2 (rider 10): edge=None (Redis down) emits through a
        process-local token bucket (≥1 event per 10s) — the outage still
        surfaces, the per-write storm does not. A fresh emission after the
        bucket interval is honored (no wall-clock sleeps: the timestamp is
        backdated)."""
        client = _FakeRedis()

        async def _raising_set(key: Any, value: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        client.set = _raising_set  # type: ignore[method-assign]
        emit = AsyncMock()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", emit),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock) as bump,
        ):
            await note_dual_write_disabled("run-1", _ORG)
            await note_dual_write_disabled("run-2", _ORG)
            await note_dual_write_disabled("run-3", _ORG)
            assert emit.await_count == 1, "token bucket: one fallback emission per interval, not one per write"
            bump.assert_not_awaited()  # the counter needs Redis; the event must not depend on it
            # Backdate the last fallback emission past the bucket interval: the
            # next write emits again (the outage keeps resurfacing).
            dualwrite_module._DEGRADED_LAST_FALLBACK_EMISSION -= (
                dualwrite_module._DEGRADED_FALLBACK_MIN_INTERVAL_SECONDS + 1.0
            )
            await note_dual_write_disabled("run-4", _ORG)
            assert emit.await_count == 2

    @pytest.mark.asyncio
    async def test_redis_down_fallback_is_silent_within_the_bucket(self, caplog: pytest.LogCaptureFixture) -> None:
        """Within the bucket interval the fallback emits NOTHING but still
        logs the degraded state (signal preserved, volume bounded)."""
        caplog.set_level("WARNING", logger="modulo.core.run_outputs_dualwrite")
        client = _FakeRedis()

        async def _raising_set(key: Any, value: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        client.set = _raising_set  # type: ignore[method-assign]
        emit = AsyncMock()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", emit),
        ):
            await note_dual_write_disabled("run-1", _ORG)
            await note_dual_write_disabled("run-2", _ORG)
        assert emit.await_count == 1
        assert any("dual_write_disabled" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_noted_window_latch_skips_all_client_churn(self) -> None:
        """qa iteration 2 (rider 10): once the edge outcome is recorded, later
        writes in the same flag-off window skip EVERYTHING (no Redis client,
        no event, no counter) — steady-state OFF must not open a client per
        legacy-only write."""
        client = _FakeRedis()
        emit = AsyncMock()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", emit),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock) as bump,
        ):
            await note_dual_write_disabled("run-1", _ORG)  # edge fires, latched
            await note_dual_write_disabled("run-2", _ORG)  # skipped entirely
            await note_dual_write_disabled("run-3", _ORG)
        assert emit.await_count == 1
        assert bump.await_count == 1
        assert len(client.set_calls) == 1, "the noted-window latch stops the per-write client churn"

    @pytest.mark.asyncio
    async def test_noted_window_latch_is_per_org(self) -> None:
        client = _FakeRedis()
        other_org = uuid.uuid4()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", new_callable=AsyncMock),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock),
        ):
            await note_dual_write_disabled("run-1", _ORG)
            await note_dual_write_disabled("run-2", other_org)
        assert len(client.set_calls) == 2, "one org's noted window cannot mask another's"

    @pytest.mark.asyncio
    async def test_edge_seen_recently_bumps_counter_without_event(self) -> None:
        client = _FakeRedis()

        async def _false_set(key: Any, value: Any, **kwargs: Any) -> Any:
            return False

        client.set = _false_set  # type: ignore[method-assign]
        emit = AsyncMock()
        with (
            patch("modulo.core.run_outputs_dualwrite._open_redis", return_value=client),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", emit),
            patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock) as bump,
        ):
            await note_dual_write_disabled("run-1", _ORG)
        emit.assert_not_awaited()
        bump.assert_awaited_once_with("outputs_dual_write_degraded")


class TestFailureEventDetailHygiene:
    """qa rider (d): the dual_write_failed event's error_detail is sanitized
    AND truncated — blob content must never land verbatim in error_events."""

    @pytest.mark.asyncio
    async def test_detail_is_sanitized_and_truncated(self) -> None:
        from modulo.core.run_outputs_dualwrite import _emit_dual_write_failed_event

        dirty = "x" * 3000 + "\x00" + "password=hunter2 secret-key=abc"
        exc = _dual_write_error(sqlstate="42501", origin="update_run_status.orm")
        captured: dict[str, Any] = {}

        async def _emit(org_id: Any, *, level: str, message: str, context_json: dict[str, Any]) -> None:
            captured["context_json"] = context_json

        with (
            patch("modulo.core.run_outputs_dualwrite._redis_incr_window", new_callable=AsyncMock, return_value=None),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", _emit),
        ):
            await _emit_dual_write_failed_event(exc, rowcount=1, error_detail=dirty)
        detail = captured["context_json"]["error_detail"]
        assert detail is not None
        assert len(detail) <= 2001, "truncated before embedding"
        assert "\x00" not in detail
        assert "hunter2" not in detail, "secret patterns are redacted"
        assert detail.endswith("…")

    @pytest.mark.asyncio
    async def test_clean_short_detail_passes_verbatim(self) -> None:
        from modulo.core.run_outputs_dualwrite import _emit_dual_write_failed_event

        exc = _dual_write_error()
        captured: dict[str, Any] = {}

        async def _emit(org_id: Any, *, level: str, message: str, context_json: dict[str, Any]) -> None:
            captured["context_json"] = context_json

        with (
            patch("modulo.core.run_outputs_dualwrite._redis_incr_window", new_callable=AsyncMock, return_value=None),
            patch("modulo.core.run_outputs_dualwrite._emit_error_event", _emit),
        ):
            await _emit_dual_write_failed_event(exc, rowcount=0, error_detail="permission denied for table runs")
        assert captured["context_json"]["error_detail"] == "permission denied for table runs"


class TestOrgLessSkipCounter:
    """qa rider (g): the org-less dual-write skip bumps its dedicated counter."""

    @pytest.mark.asyncio
    async def test_no_rls_org_skip_bumps_counter(self, sqlite_sessionmaker: Any) -> None:
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        with patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock) as bump:
            async with maker() as session, session.begin():
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"n1": {"a": 1}},
                    telemetry=None,
                )
        bump.assert_awaited_once_with("outputs_dual_write_skipped_no_org")


class TestSentinelFilteredCounterWiring:
    @pytest.mark.asyncio
    async def test_filtered_keys_bump_the_dedicated_counter(self, sqlite_sessionmaker: Any) -> None:
        """The replace helper's ``outputs_dual_write_sentinel_filtered`` count
        is wired into the dedicated counters (qa M10) — inherited sentinel
        keys are filtered (kept on the legacy column) and counted."""
        from modulo.db.crud.run import dual_write_run_node_outputs

        maker = sqlite_sessionmaker
        run_id = uuid.uuid4()
        await _seed_run(maker, run_id)
        with patch("modulo.core.run_outputs_dualwrite.bump_dual_write_counter", new_callable=AsyncMock) as bump:
            async with maker() as session, session.begin():
                await set_rls_org_for_test(session, _ORG)
                await dual_write_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=_ORG,
                    outputs={"__inherited__": {"legacy": True}, "n1": {"a": 1}},
                    telemetry=None,
                    inherited_outputs={"__inherited__": {"legacy": True}},
                )
        bumps = [(call.args[0], call.args[1]) for call in bump.await_args_list]
        assert ("outputs_dual_write_sentinel_filtered", 1) in bumps


class TestSqlstateExtraction:
    """qa iteration 2 (Major 2): the shared :mod:`modulo.db.sqlstates`
    extractor — PG fidelity for the savepoint failure chain.

    On Postgres, a transaction-aborting failure INSIDE a savepoint makes the
    savepoint's ``__aexit__`` raise the ROLLBACK-TO-SAVEPOINT failure (25P02)
    with the ORIGINAL driver error as ``__context__``. An extraction that
    only walks ``.orig``/``__cause__`` catches 25P02: the node-runner's
    transaction-aborting classification never fires on the exact failure it
    exists for, and DualWriteError.sqlstate is misattributed."""

    def test_savepoint_rollback_wrapper_does_not_mask_the_original(self) -> None:
        """25P02 (outer, raised by the savepoint __aexit__) with
        ``__context__`` = 40P01 (the original deadlock) → 40P01 wins."""
        from modulo.db.sqlstates import sqlstate_of

        original = OperationalError("stmt", {}, Exception("deadlock detected"))
        original.orig = type("_FakePG", (Exception,), {"sqlstate": "40P01"})("deadlock detected")

        class _RollbackWrapperError(OperationalError):
            """The 25P02 wrapper SQLAlchemy raises from the savepoint exit."""

        wrapper = _RollbackWrapperError("stmt", {}, Exception("current transaction is aborted"))
        wrapper.orig = type("_FakePG", (Exception,), {"sqlstate": "25P02"})("current transaction is aborted")
        wrapper.__context__ = original

        assert sqlstate_of(wrapper) == "40P01"

    def test_cause_chain_still_walks(self) -> None:
        from modulo.db.sqlstates import sqlstate_of

        original = OperationalError("stmt", {}, Exception("statement timeout"))
        original.orig = type("_FakePG", (Exception,), {"sqlstate": "57014"})("statement timeout")
        raised = RuntimeError("wrapped")
        raised.__cause__ = original
        assert sqlstate_of(raised) == "57014"

    def test_plain_25p02_with_no_other_state_is_the_fallback(self) -> None:
        """25P02 is skipped in favour of any other state in the chain; when
        the whole chain carries nothing else it is the honest answer."""
        from modulo.db.sqlstates import sqlstate_of

        wrapper = OperationalError("stmt", {}, Exception("aborted"))
        wrapper.orig = type("_FakePG", (Exception,), {"sqlstate": "25P02"})("aborted")
        assert sqlstate_of(wrapper) == "25P02"

    def test_no_sqlstate_anywhere_returns_none(self) -> None:
        from modulo.db.sqlstates import sqlstate_of

        assert sqlstate_of(RuntimeError("no sqlstate here")) is None

    def test_cycle_safe_and_bounded(self) -> None:
        from modulo.db.sqlstates import sqlstate_of

        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__context__ = b
        b.__context__ = a  # cycle
        assert sqlstate_of(a) is None

    def test_vocabularies_are_shared_not_forked(self) -> None:
        """The crud + node-runner vocabularies ARE the shared module's (the
        hoist removed both forked copies)."""
        from modulo.core.pipeline_engine.node_runner import _MARKER_TXN_ABORTING_SQLSTATES
        from modulo.db.crud.run import _DUAL_WRITE_RETRYABLE_SQLSTATES
        from modulo.db.sqlstates import DUAL_WRITE_RETRYABLE_SQLSTATES, MARKER_TXN_ABORTING_SQLSTATES

        assert _MARKER_TXN_ABORTING_SQLSTATES is MARKER_TXN_ABORTING_SQLSTATES
        assert _DUAL_WRITE_RETRYABLE_SQLSTATES is DUAL_WRITE_RETRYABLE_SQLSTATES

    def test_marker_abort_vocabulary_matches_the_shared_copy(self) -> None:
        from modulo.core.pipeline_engine.node_runner import _MARKER_TXN_ABORTING_SQLSTATES

        assert {
            "40P01",
            "57P01",
            "57P02",
            "08000",
            "08001",
            "08003",
            "08004",
            "08006",
            "08007",
        } == _MARKER_TXN_ABORTING_SQLSTATES
