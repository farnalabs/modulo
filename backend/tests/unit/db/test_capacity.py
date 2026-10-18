"""Unit tests for the DB capacity monitor + 98% hard-stop gate (FAR-425/426).

Capacity auto-detection and the source chain are FAR-1882.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db import capacity
from modulo.db.capacity import (
    ALERT_CRITICAL_PCT,
    ALERT_FULL_PCT,
    ALERT_WARN_PCT,
    StorageExhaustedError,
    _alert_level,
    capacity_hard_stop,
    db_capacity_status,
    enforce_capacity_gate,
)

# A path that cannot exist on a test machine: the "auto" chain's filesystem
# probe therefore falls through deterministically unless a test replaces
# ``_disk_usage``.
_ABSENT_PATH = "/nonexistent-modulo-test-db-volume"


@pytest.fixture(autouse=True)
def _hermetic_capacity_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the ambient state the source chain reads.

    ``_none_source_warned`` is a module-global warn-once latch, so it is pinned
    per test to stop one test's warning from suppressing another's.
    """
    monkeypatch.setattr(capacity, "_none_source_warned", False)


def _fake_settings(
    *,
    mode: str = "fixed",
    capacity_bytes: int | None = 100_000_000,
    bypass: bool = False,
    hard_stop_pct: float = 98.0,
    source: str = "auto",
    capacity_path: str = _ABSENT_PATH,
) -> SimpleNamespace:
    return SimpleNamespace(
        db_capacity_mode=mode,
        db_capacity_bytes=capacity_bytes,
        db_capacity_bypass=bypass,
        db_capacity_hard_stop_pct=hard_stop_pct,
        db_capacity_source=source,
        db_capacity_path=capacity_path,
    )


def _fake_engine(*, used_bytes: int | None = 42_000_000, raise_on_connect: bool = False) -> MagicMock:
    """Build a mock AsyncEngine whose ``connect()`` yields a scalar size.

    ``used_bytes=None`` simulates a query that returns a NULL; the caller set a
    non-None ``raise_on_connect`` to make ``connect()`` itself raise.
    """
    engine = MagicMock()

    if raise_on_connect:
        engine.connect.side_effect = RuntimeError("db unavailable")
        return engine

    result = MagicMock()
    result.scalar_one.return_value = used_bytes

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)

    conn_ctx = AsyncMock()
    conn_ctx.__aenter__ = AsyncMock(return_value=conn)
    conn_ctx.__aexit__ = AsyncMock(return_value=False)

    engine.connect.return_value = conn_ctx
    return engine


# --- alert level boundaries (80 / 90 / 98) ---------------------------------


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        pytest.param(None, "ok", id="none"),
        pytest.param(0.0, "ok", id="zero_percent"),
        pytest.param(79.9, "ok", id="just_below_warn"),
        pytest.param(80.0, "warn", id="at_warn_threshold"),
        pytest.param(89.9, "warn", id="just_below_critical"),
        pytest.param(90.0, "critical", id="at_critical_threshold"),
        pytest.param(97.9, "critical", id="just_below_full"),
        pytest.param(98.0, "full", id="at_full_threshold"),
        pytest.param(100.0, "full", id="max_percent"),
    ],
)
def test_alert_level_boundaries(percent: float | None, expected: str) -> None:
    assert _alert_level(percent) == expected


@pytest.mark.parametrize("threshold", [ALERT_WARN_PCT, ALERT_CRITICAL_PCT, ALERT_FULL_PCT])
def test_alert_constants_match_spec(threshold: float) -> None:
    assert threshold in (80.0, 90.0, 98.0)


# --- db_capacity_status -----------------------------------------------------


async def test_status_reports_used_and_percent_for_fixed_mode() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=100_000_000)):
        status = await db_capacity_status(engine)

    assert status["mode"] == "fixed"
    assert status["used_bytes"] == 50_000_000
    assert status["capacity_bytes"] == 100_000_000
    assert status["capacity_percent"] == 50.0
    assert status["alert_level"] == "ok"


async def test_status_percent_clamped_to_100() -> None:
    # The clamp is a DEFENSIVE bound: in normal operation no source produces
    # used > capacity (an auto-detected filesystem reading uses total - free <=
    # total, and the impossibility guard discards a reading smaller than the DB
    # it holds), so an over-capacity numerator is pinned here by resolving one
    # directly.
    engine = _fake_engine(used_bytes=150_000_000)
    resolved = capacity._ResolvedCapacity(100_000_000, 150_000_000, "env")
    with (
        patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=100_000_000)),
        patch("modulo.db.capacity._resolve_capacity", new=MagicMock(return_value=resolved)),
    ):
        status = await db_capacity_status(engine)
    assert status["capacity_percent"] == 100.0
    assert status["alert_level"] == "full"


async def test_status_percent_none_when_no_capacity_configured() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=None)):
        status = await db_capacity_status(engine)
    assert status["capacity_percent"] is None
    assert status["capacity_bytes"] is None
    assert status["source"] == "none"
    assert status["alert_level"] == "ok"


async def test_status_elastic_reports_used_but_no_capacity() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(mode="elastic")):
        status = await db_capacity_status(engine)
    assert status["mode"] == "elastic"
    assert status["used_bytes"] == 50_000_000
    assert status["capacity_bytes"] is None
    assert status["source"] == "none"
    assert status["capacity_percent"] is None
    assert status["alert_level"] == "ok"


async def test_status_disabled_skips_the_query() -> None:
    engine = _fake_engine()
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(mode="disabled")):
        status = await db_capacity_status(engine)
    assert status["mode"] == "disabled"
    assert status["used_bytes"] == 0
    assert status["capacity_bytes"] is None
    engine.connect.assert_not_called()


async def test_status_resilient_when_query_fails() -> None:
    engine = _fake_engine(raise_on_connect=True)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings()):
        status = await db_capacity_status(engine)
    assert status["capacity_percent"] is None
    assert status["alert_level"] == "ok"


async def test_status_treats_unknown_mode_as_disabled() -> None:
    engine = _fake_engine()
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(mode="bogus")):
        status = await db_capacity_status(engine)
    assert status["mode"] == "disabled"


# --- capacity sources (FAR-1882) -------------------------------------------


async def test_source_env_when_the_override_is_configured() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=100_000_000)):
        status = await db_capacity_status(engine)
    assert status["source"] == "env"
    assert status["capacity_bytes"] == 100_000_000
    # The numerator for the env source is still pg_database_size.
    assert status["used_bytes"] == 50_000_000
    assert status["capacity_percent"] == 50.0


async def test_source_env_override_wins_over_filesystem() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=10_000_000_000, free=1_000_000_000))
    settings = _fake_settings(capacity_bytes=100_000_000, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "env"
    assert status["capacity_bytes"] == 100_000_000
    # The lower-priority source is not even consulted once one resolves.
    disk_usage.assert_not_called()


async def test_source_filesystem_when_the_override_is_unset() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=100_000_000, free=20_000_000))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    disk_usage.assert_called_once_with("/vol")
    assert status["source"] == "filesystem"
    assert status["capacity_bytes"] == 100_000_000
    # A whole-volume capacity is honestly paired with the volume's own usage
    # (total - free), not with pg_database_size.
    assert status["used_bytes"] == 80_000_000
    assert status["capacity_percent"] == 80.0
    assert status["alert_level"] == "warn"


async def test_source_none_when_no_source_resolves() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    disk_usage = MagicMock(side_effect=OSError("absent"))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "none"
    assert status["capacity_bytes"] is None
    assert status["capacity_percent"] is None
    assert status["alert_level"] == "ok"


async def test_source_disabled_resolves_nothing_even_with_the_override_set() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    settings = _fake_settings(capacity_bytes=100_000_000, source="disabled")
    with patch("modulo.db.capacity.get_settings", return_value=settings):
        status = await db_capacity_status(engine)
    assert status["source"] == "none"
    assert status["capacity_bytes"] is None
    assert status["capacity_percent"] is None
    assert status["alert_level"] == "ok"


async def test_source_env_pins_the_override_only() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=100_000_000, free=20_000_000))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol", source="env")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "none"
    disk_usage.assert_not_called()


async def test_source_filesystem_ignores_the_override() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=100_000_000, free=80_000_000))
    settings = _fake_settings(capacity_bytes=999_000_000, capacity_path="/vol", source="filesystem")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "filesystem"
    assert status["capacity_bytes"] == 100_000_000
    assert status["used_bytes"] == 20_000_000


async def test_unrecognised_source_falls_back_to_auto() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    settings = _fake_settings(capacity_bytes=100_000_000, source="bogus")
    with patch("modulo.db.capacity.get_settings", return_value=settings):
        status = await db_capacity_status(engine)
    assert status["source"] == "env"


async def test_elastic_mode_never_resolves_a_source() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    settings = _fake_settings(mode="elastic", capacity_bytes=100_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=settings):
        status = await db_capacity_status(engine)
    assert status["source"] == "none"
    assert status["capacity_bytes"] is None


# --- the impossibility guard, scoped to auto-detected sources (FAR-1882) ----


async def test_env_override_is_never_second_guessed() -> None:
    # An explicit override is the operator asserting the capacity, so even a
    # value well below the measured DB size is trusted (the DB size can be read
    # from a source the operator does not consider the volume).
    engine = _fake_engine(used_bytes=200_000_000)
    settings = _fake_settings(capacity_bytes=100_000_000, source="env")
    with patch("modulo.db.capacity.get_settings", return_value=settings):
        status = await db_capacity_status(engine)
    assert status["source"] == "env"
    assert status["capacity_bytes"] == 100_000_000
    # used > capacity clamps to 100, and the gate is armed.
    assert status["capacity_percent"] == 100.0
    assert status["alert_level"] == "full"
    assert capacity_hard_stop(settings, status) is True


async def test_env_override_at_98_percent_refuses_a_new_run() -> None:
    # The regression the old guard reintroduced: an explicit override at the
    # threshold must still refuse a new run.
    engine = _fake_engine(used_bytes=98_000_000)
    settings = _fake_settings(capacity_bytes=100_000_000, source="env")
    with patch("modulo.db.capacity.get_settings", return_value=settings):
        status = await db_capacity_status(engine)
    assert status["source"] == "env"
    assert status["capacity_percent"] == 98.0
    assert capacity_hard_stop(settings, status) is True
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        pytest.raises(StorageExhaustedError),
    ):
        await enforce_capacity_gate(settings=settings, engine=engine)


async def test_filesystem_reading_smaller_than_the_database_is_discarded() -> None:
    # Physically impossible for a real volume to be smaller than the database it
    # holds, so the reading is the wrong filesystem: discarded, not trusted. No
    # source then resolves, so the monitor falls through to "none" (fail-open -
    # it never raises).
    engine = _fake_engine(used_bytes=200_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=100_000_000, free=60_000_000))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "none"
    assert status["capacity_bytes"] is None
    assert status["capacity_percent"] is None
    assert status["alert_level"] == "ok"
    assert capacity_hard_stop(settings, status) is False


async def test_filesystem_reading_just_above_the_database_still_arms_the_gate() -> None:
    # A legitimately small-but-valid volume (total barely above the DB it holds)
    # must NOT be discarded: the check is an impossibility, not a margin. Under
    # the old "not plausibly larger" 105% margin this reading would have been
    # thrown away - the gate silently disarmed exactly when it mattered.
    engine = _fake_engine(used_bytes=100_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=101_000_000, free=1_010_000))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "filesystem"
    assert status["capacity_bytes"] == 101_000_000
    assert status["capacity_percent"] == 99.0
    assert capacity_hard_stop(settings, status) is True
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
        pytest.raises(StorageExhaustedError),
    ):
        await enforce_capacity_gate(settings=settings, engine=engine)


async def test_env_override_resolves_when_the_database_size_is_unknown() -> None:
    # No measurement to compare against: the override still resolves (fail-open).
    engine = _fake_engine(raise_on_connect=True)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=100_000_000)):
        status = await db_capacity_status(engine)
    assert status["source"] == "env"
    assert status["capacity_bytes"] == 100_000_000
    assert status["capacity_percent"] is None
    assert status["alert_level"] == "ok"


async def test_filesystem_is_not_discarded_when_the_database_size_is_unknown() -> None:
    # Without a DB measurement the impossibility check cannot fire, so the
    # filesystem reading is trusted (fail-open).
    engine = _fake_engine(raise_on_connect=True)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=100_000_000, free=20_000_000))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        status = await db_capacity_status(engine)
    assert status["source"] == "filesystem"
    assert status["capacity_bytes"] == 100_000_000
    assert status["used_bytes"] == 80_000_000


async def test_enforce_raises_on_an_auto_detected_full_volume() -> None:
    # No DB_CAPACITY_BYTES anywhere: the 98% stop is armed by the volume probe.
    engine = _fake_engine(used_bytes=1_000_000_000)
    disk_usage = MagicMock(return_value=SimpleNamespace(total=100_000_000_000, free=2_000_000_000))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
        pytest.raises(StorageExhaustedError),
    ):
        await enforce_capacity_gate(settings=settings, engine=engine)


async def test_enforce_allows_when_auto_detection_resolves_nothing() -> None:
    # Fail-open, unchanged: an unresolvable capacity never blocks a run.
    engine = _fake_engine(used_bytes=50_000_000)
    disk_usage = MagicMock(side_effect=OSError("absent"))
    settings = _fake_settings(capacity_bytes=None, capacity_path="/vol")
    with (
        patch("modulo.db.capacity.get_settings", return_value=settings),
        patch("modulo.db.capacity._disk_usage", new=disk_usage),
    ):
        result = await enforce_capacity_gate(settings=settings, engine=engine)
    assert result is None


# --- _probe_filesystem_capacity --------------------------------------------


def test_filesystem_probe_returns_none_on_a_filesystem_error() -> None:
    with patch("modulo.db.capacity._disk_usage", new=MagicMock(side_effect=OSError("nope"))):
        assert capacity._probe_filesystem_capacity("/vol") is None


def test_filesystem_probe_rejects_a_zero_total() -> None:
    with patch(
        "modulo.db.capacity._disk_usage",
        new=MagicMock(return_value=SimpleNamespace(total=0, free=0)),
    ):
        assert capacity._probe_filesystem_capacity("/vol") is None


def test_filesystem_probe_clamps_a_negative_free_to_zero() -> None:
    with patch(
        "modulo.db.capacity._disk_usage",
        new=MagicMock(return_value=SimpleNamespace(total=100, free=-5)),
    ):
        assert capacity._probe_filesystem_capacity("/vol") == (100, 0)


# --- _try_source / _warn_no_capacity_source --------------------------------


def test_try_source_returns_none_for_an_unknown_candidate() -> None:
    # Defensive fall-through: a candidate that is neither ``env`` nor
    # ``filesystem`` resolves nothing (fail-open) rather than raising, so a
    # future source name cannot crash a dispatch by being unhandled.
    assert capacity._try_source("bogus", _fake_settings(capacity_bytes=100_000_000), None) is None


def test_warn_no_capacity_source_is_warn_once() -> None:
    # The latch is a module-global warn-once: the second call is silent so a
    # repeatedly-polled monitor cannot flood the log.
    with patch("modulo.db.capacity._log") as log:
        capacity._warn_no_capacity_source("/vol", "auto")
        capacity._warn_no_capacity_source("/vol", "auto")
    assert log.warning.call_count == 1


# --- capacity_hard_stop (mode gating) --------------------------------------


def test_hard_stop_fixed_at_threshold() -> None:
    assert capacity_hard_stop(_fake_settings(mode="fixed"), {"capacity_percent": 98.0}) is True


def test_hard_stop_fixed_above_threshold() -> None:
    assert capacity_hard_stop(_fake_settings(mode="fixed"), {"capacity_percent": 99.5}) is True


def test_hard_stop_fixed_below_threshold() -> None:
    assert capacity_hard_stop(_fake_settings(mode="fixed"), {"capacity_percent": 97.9}) is False


def test_hard_stop_unknown_percent_allowed() -> None:
    assert capacity_hard_stop(_fake_settings(mode="fixed"), {"capacity_percent": None}) is False


def test_hard_stop_elastic_never() -> None:
    assert capacity_hard_stop(_fake_settings(mode="elastic"), {"capacity_percent": 100.0}) is False


def test_hard_stop_disabled_never() -> None:
    assert capacity_hard_stop(_fake_settings(mode="disabled"), {"capacity_percent": 100.0}) is False


def test_hard_stop_bypass_allows() -> None:
    assert capacity_hard_stop(_fake_settings(mode="fixed", bypass=True), {"capacity_percent": 100.0}) is False


# --- enforce_capacity_gate --------------------------------------------------


def _patch_capacity(status: dict, settings) -> tuple[Any, Any]:
    get_settings = patch("modulo.db.capacity.get_settings", return_value=settings)
    db_status = patch("modulo.db.capacity.db_capacity_status", new=AsyncMock(return_value=status))
    return get_settings, db_status


async def test_enforce_raises_when_fixed_and_98() -> None:
    settings = _fake_settings(mode="fixed")
    get_settings, db_status = _patch_capacity({"capacity_percent": 98.0}, settings)
    with get_settings, db_status, pytest.raises(StorageExhaustedError):
        await enforce_capacity_gate(engine=_fake_engine())


async def test_enforce_allows_below_threshold() -> None:
    settings = _fake_settings(mode="fixed")
    get_settings, db_status = _patch_capacity({"capacity_percent": 97.0}, settings)
    with get_settings, db_status:
        result = await enforce_capacity_gate(engine=_fake_engine())  # must not raise
    assert result is None


async def test_enforce_allows_elastic() -> None:
    settings = _fake_settings(mode="elastic")
    get_settings, db_status = _patch_capacity({"capacity_percent": 100.0}, settings)
    with get_settings, db_status:
        result = await enforce_capacity_gate(engine=_fake_engine())
    assert result is None


async def test_enforce_allows_bypass() -> None:
    settings = _fake_settings(mode="fixed", bypass=True)
    get_settings, db_status = _patch_capacity({"capacity_percent": 100.0}, settings)
    with get_settings, db_status:
        result = await enforce_capacity_gate(engine=_fake_engine())
    assert result is None


async def test_enforce_allows_on_measurement_failure() -> None:
    settings = _fake_settings(mode="fixed")
    get_settings = patch("modulo.db.capacity.get_settings", return_value=settings)
    db_status = patch(
        "modulo.db.capacity.db_capacity_status",
        new=AsyncMock(side_effect=RuntimeError("measurement broke")),
    )
    with get_settings, db_status:
        result = await enforce_capacity_gate(engine=_fake_engine())  # fail-open: must not raise
    assert result is None


# --- create_run propagates StorageExhaustedError; API maps it to 503 -------


async def test_create_run_propagates_storage_exhausted() -> None:
    from modulo.db.crud import run as run_crud

    session = AsyncMock()
    with (
        patch.object(run_crud, "_ensure_org_not_deleted", new=AsyncMock()),
        patch(
            "modulo.db.capacity.enforce_capacity_gate",
            new=AsyncMock(side_effect=StorageExhaustedError("full")),
        ),
        pytest.raises(StorageExhaustedError),
    ):
        await run_crud.create_run(
            session,
            org_id=MagicMock(),
            pipeline_id=MagicMock(),
            snapshot_id=MagicMock(),
            trigger_type="manual",
            input_payload={},
        )


def test_storage_exhausted_handler_returns_503() -> None:
    # The API-layer handler maps StorageExhaustedError → 503
    # urn:problem:modulo:storage_exhausted (FAR-426 "specific exception → 503").
    from modulo.api.exception_handlers import storage_exhausted_exception_handler

    request = MagicMock()
    request.state.request_id = "req-1"
    request.url.path = "/api/v1/runs"

    response = storage_exhausted_exception_handler(request, StorageExhaustedError("full"))
    assert response.status_code == 503
    assert response.body.decode().startswith("{")
    assert "storage_exhausted" in response.body.decode()
    assert "full" in response.body.decode()
