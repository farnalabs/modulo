"""Unit tests for the DB capacity monitor + 98% hard-stop gate (FAR-425/426)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _fake_settings(
    *,
    mode: str = "fixed",
    capacity_bytes: int | None = 100_000_000,
    bypass: bool = False,
    hard_stop_pct: float = 98.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        db_capacity_mode=mode,
        db_capacity_bytes=capacity_bytes,
        db_capacity_bypass=bypass,
        db_capacity_hard_stop_pct=hard_stop_pct,
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
    ids=[
        "none_ok",
        "zero_ok",
        "below_warn",
        "at_warn",
        "below_critical",
        "at_critical",
        "below_full",
        "at_full",
        "at_100",
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
    engine = _fake_engine(used_bytes=150_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=100_000_000)):
        status = await db_capacity_status(engine)
    assert status["capacity_percent"] == 100.0
    assert status["alert_level"] == "full"


async def test_status_percent_none_when_no_capacity_configured() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(capacity_bytes=None)):
        status = await db_capacity_status(engine)
    assert status["capacity_percent"] is None
    assert status["capacity_bytes"] is None
    assert status["alert_level"] == "ok"


async def test_status_elastic_reports_used_but_no_capacity() -> None:
    engine = _fake_engine(used_bytes=50_000_000)
    with patch("modulo.db.capacity.get_settings", return_value=_fake_settings(mode="elastic")):
        status = await db_capacity_status(engine)
    assert status["mode"] == "elastic"
    assert status["used_bytes"] == 50_000_000
    assert status["capacity_bytes"] is None
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
    run_crud._ensure_org_not_deleted = AsyncMock()  # type: ignore[attr-defined]
    with (
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


async def test_storage_exhausted_handler_returns_503() -> None:
    # The API-layer handler maps StorageExhaustedError → 503
    # urn:problem:modulo:storage_exhausted (FAR-426 "specific exception → 503").
    from modulo.api.exception_handlers import storage_exhausted_exception_handler

    request = MagicMock()
    request.state.request_id = "req-1"
    request.url.path = "/api/v1/runs"

    response = await storage_exhausted_exception_handler(request, StorageExhaustedError("full"))
    assert response.status_code == 503
    assert response.body.decode().startswith("{")
    assert "storage_exhausted" in response.body.decode()
    assert "full" in response.body.decode()
