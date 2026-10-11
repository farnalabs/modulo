"""Unit tests for the FAR-1175 live-read seams (no DB).

The HITL gate resolves earned autonomy at gate time by calling two helpers:

* ``read_live_earned_autonomy`` — reads the pipeline's live
  ``earned_autonomy_level`` (fail-open; any failure degrades to ``None``), and
* ``is_autonomy_gating_enabled`` — resolves the ``autonomy_gating`` feature
  flag (fail-open; default OFF).

Both are fail-open by design: a telemetry/state read must never break a run,
and a flag-read failure must degrade to "today's behaviour", never to an
accidental widening of autonomy. These tests pin that contract with a fake
session factory and patched crud/flag seams — the gate-time wiring that calls
these helpers is exercised in
``tests/unit/pipeline_engine/test_node_runner_hitl.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.run_context import autonomy as at

pytestmark = pytest.mark.asyncio


class _NullBegin:
    """Async context manager returned by ``_NullSession.begin()``."""

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _NullSession:
    """Minimal session stand-in: supports ``async with factory() as s, s.begin():``."""

    def begin(self) -> _NullBegin:
        return _NullBegin()

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _session_factory() -> _NullSession:
    return _NullSession()


def _pipeline(earned: Any) -> MagicMock:
    pipeline = MagicMock()
    pipeline.earned_autonomy_level = earned
    return pipeline


def _patch_rls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("modulo.db.rls.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.db.rls.set_rls_execution_context", AsyncMock())


class TestReadLiveEarnedAutonomy:
    """``read_live_earned_autonomy`` reads the live pipeline row, fail-open."""

    async def test_returns_none_without_session_factory(self) -> None:
        result = await at.read_live_earned_autonomy(None, org_id=uuid.uuid4(), pipeline_id=uuid.uuid4())
        assert result is None

    async def test_returns_none_without_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_rls(monkeypatch)
        get_pipeline = AsyncMock(return_value=_pipeline("fully_autonomous"))
        monkeypatch.setattr("modulo.db.crud.pipeline.get_pipeline", get_pipeline)

        assert await at.read_live_earned_autonomy(_session_factory, org_id=None, pipeline_id=uuid.uuid4()) is None
        assert await at.read_live_earned_autonomy(_session_factory, org_id=uuid.uuid4(), pipeline_id=None) is None
        get_pipeline.assert_not_awaited()

    async def test_reads_the_live_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_rls(monkeypatch)
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        get_pipeline = AsyncMock(return_value=_pipeline("notify_on_complete"))
        monkeypatch.setattr("modulo.db.crud.pipeline.get_pipeline", get_pipeline)

        result = await at.read_live_earned_autonomy(_session_factory, org_id=org_id, pipeline_id=pipeline_id)

        assert result == "notify_on_complete"
        get_pipeline.assert_awaited_once()
        # The read is org-scoped (never a cross-tenant lookup).
        assert get_pipeline.await_args.kwargs["organisation_id"] == org_id
        assert get_pipeline.await_args.args[1] == pipeline_id

    async def test_missing_pipeline_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_rls(monkeypatch)
        monkeypatch.setattr("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=None))
        result = await at.read_live_earned_autonomy(_session_factory, org_id=uuid.uuid4(), pipeline_id=uuid.uuid4())
        assert result is None

    @pytest.mark.parametrize("earned", [None, "", 42, ["fully_autonomous"]])
    async def test_non_string_or_empty_level_returns_none(self, monkeypatch: pytest.MonkeyPatch, earned: Any) -> None:
        _patch_rls(monkeypatch)
        monkeypatch.setattr("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=_pipeline(earned)))
        result = await at.read_live_earned_autonomy(_session_factory, org_id=uuid.uuid4(), pipeline_id=uuid.uuid4())
        assert result is None

    async def test_db_error_fails_open_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_rls(monkeypatch)
        monkeypatch.setattr(
            "modulo.db.crud.pipeline.get_pipeline",
            AsyncMock(side_effect=RuntimeError("db down")),
        )
        result = await at.read_live_earned_autonomy(_session_factory, org_id=uuid.uuid4(), pipeline_id=uuid.uuid4())
        assert result is None

    async def test_cancellation_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_rls(monkeypatch)
        monkeypatch.setattr(
            "modulo.db.crud.pipeline.get_pipeline",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            await at.read_live_earned_autonomy(_session_factory, org_id=uuid.uuid4(), pipeline_id=uuid.uuid4())


class _FlagRegistry:
    """Feature-flag registry double recording ``resolve_flag`` calls."""

    def __init__(self, value: bool) -> None:
        self.value = value
        self.calls: list[tuple[str, Any]] = []

    async def resolve_flag(self, flag_name: str, org_id: Any = None, **_kwargs: Any) -> bool:
        self.calls.append((flag_name, org_id))
        return self.value


class TestIsAutonomyGatingEnabled:
    """``is_autonomy_gating_enabled`` resolves the flag, defaulting OFF."""

    async def test_resolves_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = _FlagRegistry(True)
        monkeypatch.setattr("modulo.core.feature_flags.get_registry", lambda: registry)
        org_id = uuid.uuid4()

        assert await at.is_autonomy_gating_enabled(org_id=org_id) is True
        assert registry.calls == [(at.AUTONOMY_GATING_FLAG, org_id)]

    async def test_flag_off_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("modulo.core.feature_flags.get_registry", lambda: _FlagRegistry(False))
        assert await at.is_autonomy_gating_enabled(org_id=uuid.uuid4()) is False

    async def test_registry_error_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr("modulo.core.feature_flags.get_registry", _boom)
        assert await at.is_autonomy_gating_enabled(org_id=uuid.uuid4()) is False

    async def test_cancellation_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr("modulo.core.feature_flags.get_registry", _boom)
        with pytest.raises(asyncio.CancelledError):
            await at.is_autonomy_gating_enabled(org_id=uuid.uuid4())
