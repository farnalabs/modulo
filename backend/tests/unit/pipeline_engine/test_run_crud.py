"""Unit tests for run CRUD helpers (no DB — pure functions and fail-open readers).

Covers ``_input_hash`` (stable digest contract), the active-run status set,
the zero-shaped stats response, and the org-level integer-limit reader
(fail-open paths, type rejection, range clamping) used by the capacity gates.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.db.crud.run import (
    _active_run_statuses,
    _empty_run_stats,
    _input_hash,
    _read_org_int_limit,
)

# ---------------------------------------------------------------------------
# _input_hash
# ---------------------------------------------------------------------------


def test_input_hash_is_deterministic():
    payload = {"key": "value", "num": 42}
    # Golden value pins the exact digest so a change in sort_keys/separators
    # or hash algorithm fails loudly instead of silently corrupting dedup.
    assert _input_hash(payload) == "d81188885389ad7836eebf580ec0a1c85d4b987810edfa41df659ab13c2bc50b"


def test_input_hash_is_order_independent():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert _input_hash(a) == _input_hash(b)


def test_input_hash_is_order_independent_nested():
    a = {"b": {"d": 1, "c": 2}, "a": [3, 1, 2]}
    b = {"a": [3, 1, 2], "b": {"c": 2, "d": 1}}
    assert _input_hash(a) == _input_hash(b)


def test_input_hash_differs_for_different_payloads():
    assert _input_hash({"x": 1}) != _input_hash({"x": 2})


def test_input_hash_empty_payload():
    h = _input_hash({})
    assert len(h) == 64  # SHA-256 hex digest
    # Golden value: pins the exact digest so a change in sort_keys/separators
    # or hash algorithm fails loudly instead of silently corrupting run dedup.
    assert h == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


def test_input_hash_uses_compact_separators():
    """The digest must be computed over compact JSON separators, not pretty output."""
    compact = hashlib.sha256(b'{"k":"v"}').hexdigest()
    assert _input_hash({"k": "v"}) == compact


def test_input_hash_default_str_for_non_serializable():
    """Non-JSON-serialisable values fall back to ``str`` instead of raising."""
    payload = {"ts": datetime(2024, 1, 1, tzinfo=UTC)}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    assert _input_hash(payload) == expected
    assert _input_hash(payload) == "691064efc26716e470ce0fc328efa5c98e3b82c67d8456481b0255e47b8a5bda"


# ---------------------------------------------------------------------------
# _active_run_statuses
# ---------------------------------------------------------------------------


def test_active_run_statuses_includes_pending_when_requested():
    assert _active_run_statuses(True) == {
        "pending",
        "running",
        "awaiting_human",
        "claimed",
        "unknown",
    }


def test_active_run_statuses_excludes_pending_for_capacity():
    assert _active_run_statuses(False) == {
        "running",
        "awaiting_human",
        "claimed",
        "unknown",
    }
    assert "pending" not in _active_run_statuses(False)


def test_active_run_statuses_returns_fresh_set():
    result = _active_run_statuses(True)
    result.discard("pending")
    assert "pending" in _active_run_statuses(True)


# ---------------------------------------------------------------------------
# _empty_run_stats
# ---------------------------------------------------------------------------


def test_empty_run_stats_zero_shape():
    """The empty-window response must keep the same shape as a populated one."""
    assert _empty_run_stats() == {
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0,
        "p50_duration_ms": 0,
        "p95_duration_ms": 0,
        "p99_duration_ms": 0,
        "runs_by_day": [],
        "failure_by_reason": [],
        "avg_duration_by_day": [],
    }


# ---------------------------------------------------------------------------
# _read_org_int_limit — fail-open capacity reader
# ---------------------------------------------------------------------------

_LOG_PREFIX = "sandbox_concurrency"
_KEY = "sandbox_concurrency_limit"
_MIN = 1
_MAX = 100


def _org_with(settings: object) -> SimpleNamespace:
    return SimpleNamespace(settings_json=settings)


async def _read_limit(settings: object, *, org: object | None = None) -> int | None:
    session = AsyncMock()
    with patch(
        "modulo.db.crud.run.get_organisation",
        AsyncMock(return_value=_org_with(settings) if org is None else org),
    ):
        return await _read_org_int_limit(session, uuid.uuid4(), _KEY, _MIN, _MAX, _LOG_PREFIX)


async def test_read_int_limit_valid_value(caplog: pytest.LogCaptureFixture) -> None:
    limit = await _read_limit({_KEY: 4})
    assert limit == 4
    assert "out_of_range" not in caplog.text


async def test_read_int_limit_missing_key_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    limit = await _read_limit({"some_other_setting": 10})
    assert limit is None
    assert "invalid_type" not in caplog.text


async def test_read_int_limit_missing_org_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    session = AsyncMock()
    with patch("modulo.db.crud.run.get_organisation", AsyncMock(return_value=None)):
        limit = await _read_org_int_limit(session, uuid.uuid4(), _KEY, _MIN, _MAX, _LOG_PREFIX)
    assert limit is None
    assert f"{_LOG_PREFIX}.org_not_found" in caplog.text


async def test_read_int_limit_non_dict_settings_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    for bad in ("10", 10, [], None, True):
        assert await _read_limit(bad) is None, f"settings={bad!r}"
    assert f"{_LOG_PREFIX}.settings_not_dict" in caplog.text


@pytest.mark.parametrize("bad", [True, False, "10", 10.5])
async def test_read_int_limit_rejects_non_int_types(bad: object, caplog: pytest.LogCaptureFixture) -> None:
    limit = await _read_limit({_KEY: bad})
    assert limit is None
    assert f"{_LOG_PREFIX}.invalid_type" in caplog.text


async def test_read_int_limit_clamps_below_min(caplog: pytest.LogCaptureFixture) -> None:
    limit = await _read_limit({_KEY: 0})
    assert limit == _MIN
    assert f"{_LOG_PREFIX}.out_of_range" in caplog.text


async def test_read_int_limit_clamps_above_max(caplog: pytest.LogCaptureFixture) -> None:
    limit = await _read_limit({_KEY: 500})
    assert limit == _MAX
    assert f"{_LOG_PREFIX}.out_of_range" in caplog.text


async def test_read_int_limit_returns_boundary_values(caplog: pytest.LogCaptureFixture) -> None:
    assert await _read_limit({_KEY: _MIN}) == _MIN
    assert await _read_limit({_KEY: _MAX}) == _MAX
    assert "out_of_range" not in caplog.text
