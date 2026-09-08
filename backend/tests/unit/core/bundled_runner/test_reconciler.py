"""Unit tests for the Docker workspace-orphan reconciler sweep (FAR-590 D4).

Covers the committed semantics: grace period, log-only soak default,
destroy-mode orphan removal, active-run spare + destroy-path false-positive
re-check, the 24h max-lifetime backstop (applies regardless of run state),
fail-safe abort on cross-reference failure, and machine-scoped container
filters.
"""

import logging
import uuid
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.bundled_runner import runner_reconciler
from modulo.core.bundled_runner.runner_reconciler import (
    ReconcilerSweepError,
    _LabelledContainer,
    deployment_identity,
    reconcile_runner_workspaces,
)


def _container(run_id: str, age_s: float, cid: str = "c-1") -> _LabelledContainer:
    return _LabelledContainer(id=cid, run_id=run_id, created_age_s=age_s)


def _fake_source(containers: list[_LabelledContainer]) -> MagicMock:
    source = MagicMock()
    source.list_labelled_workspaces = AsyncMock(return_value=containers)
    source.destroy_by_container_id = AsyncMock()
    source.close = AsyncMock()
    return source


def _engine_with_active_runs(run_ids: list[str], *, fail: bool = False) -> MagicMock:
    engine = MagicMock()

    class _Conn:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, *_a: object, **_k: object):
            if fail:
                raise RuntimeError("db blip")
            rows = [[rid] for rid in run_ids]
            return SimpleNamespace(all=lambda: rows, fetchone=lambda: rows[0] if rows else None)

    engine.connect = lambda: _Conn()
    return engine


def _settings(destroy_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(runner_reconciler_destroy_enabled=destroy_enabled)


async def _sweep(
    monkeypatch: pytest.MonkeyPatch,
    containers: list[_LabelledContainer],
    *,
    destroy: bool,
    engine: MagicMock | None = None,
) -> dict[str, int]:
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: _fake_source(containers))
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(destroy))
    return await reconcile_runner_workspaces(engine or _engine_with_active_runs([]))


async def test_log_only_soak_default_never_destroys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        result = await _sweep(monkeypatch, [_container("gone-run", 9999)], destroy=False)
    assert result == {"scanned": 1, "orphans_destroyed": 0}
    assert "orphan_detected" in caplog.text


async def test_destroy_enabled_destroys_orphan_past_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _fake_source([_container("gone-run", 9999, "orphan-1")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))

    result = await reconcile_runner_workspaces(_engine_with_active_runs([]))

    assert result == {"scanned": 1, "orphans_destroyed": 1}
    source.destroy_by_container_id.assert_has_awaits([(("orphan-1",), {})])


async def test_orphan_within_grace_is_never_destroyed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _fake_source([_container("gone-run", 10.0, "recent-1")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))

    result = await reconcile_runner_workspaces(_engine_with_active_runs([]))

    assert result == {"scanned": 1, "orphans_destroyed": 0}
    source.destroy_by_container_id.assert_not_awaited()


async def test_container_without_created_marker_is_never_destroyed(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _fake_source([_container("gone-run", 0.0, "no-marker")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))

    result = await reconcile_runner_workspaces(_engine_with_active_runs([]))

    assert result == {"scanned": 1, "orphans_destroyed": 0}
    source.destroy_by_container_id.assert_not_awaited()


async def test_active_run_container_is_spared(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = str(uuid.uuid4())
    source = _fake_source([_container(run_id, 9999, "active-1")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))

    result = await reconcile_runner_workspaces(_engine_with_active_runs([run_id]))

    assert result == {"scanned": 1, "orphans_destroyed": 0}
    source.destroy_by_container_id.assert_not_awaited()


async def test_active_run_container_past_max_lifetime_is_reclaimed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The 24h backstop applies regardless of run state (D1 kill path)."""
    run_id = str(uuid.uuid4())
    source = _fake_source([_container(run_id, 90000.0, "expired-1")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))
    engine = _engine_with_active_runs([run_id])

    with caplog.at_level(logging.WARNING):
        result = await reconcile_runner_workspaces(engine)

    assert result["orphans_destroyed"] == 1
    source.destroy_by_container_id.assert_has_awaits([(("expired-1",), {})])
    assert "reclaimed_max_lifetime" in caplog.text


async def test_destroy_path_recheck_detects_false_positive(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Run appeared active between the bulk query and the destroy re-check —
    the sweep spares the container and logs the D4 rollback signal."""
    source = _fake_source([_container("raced-run", 9999, "race-1")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))
    # Bulk query sees nothing active; the per-container re-check DOES.
    engine = _engine_with_active_runs([])
    calls = {"n": 0}

    original_connect = engine.connect

    def _connect() -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return original_connect()
        return _RunActiveConn()

    engine.connect = _connect

    with caplog.at_level(logging.WARNING):
        result = await reconcile_runner_workspaces(engine)

    assert result == {"scanned": 1, "orphans_destroyed": 0}
    source.destroy_by_container_id.assert_not_awaited()
    assert "suspected_false_positive" in caplog.text


class _RunActiveConn:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, *_a: object, **_k: object):
        return SimpleNamespace(fetchone=lambda: (1,))


async def test_cross_reference_failure_aborts_sweep_destroys_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = _fake_source([_container("gone-run", 9999, "orphan-9")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))

    with caplog.at_level(logging.ERROR), pytest.raises(ReconcilerSweepError, match="cross-reference"):
        await reconcile_runner_workspaces(_engine_with_active_runs([], fail=True))

    source.destroy_by_container_id.assert_not_awaited()
    assert "sweep_aborted" in caplog.text


async def test_container_list_failure_aborts_before_cross_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _fake_source([])
    source.list_labelled_workspaces = AsyncMock(side_effect=OSError("engine unreachable"))
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(True))

    with pytest.raises(ReconcilerSweepError, match="listing containers"):
        await reconcile_runner_workspaces(_engine_with_active_runs([]))

    source.destroy_by_container_id.assert_not_awaited()


async def test_log_only_max_lifetime_is_logged_not_destroyed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = _fake_source([_container("gone-run", 90000.0, "old-1")])
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", lambda host: source)
    monkeypatch.setattr("modulo.settings.get_settings", lambda: _settings(False))

    result = await reconcile_runner_workspaces(_engine_with_active_runs([]))

    assert result == {"scanned": 1, "orphans_destroyed": 0}
    source.destroy_by_container_id.assert_not_awaited()
    assert "reclaimed_max_lifetime" in caplog.text


# ---------------------------------------------------------------------------
# Machine-scoped Docker source (deployment-identity label filter)
# ---------------------------------------------------------------------------


async def test_docker_source_filters_on_deployment_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container listing rides the modulo.machine.id label so two
    deployments sharing one engine never destroy each other's workspaces."""
    monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "deployment-a")
    client = MagicMock()
    client.containers.list = AsyncMock(return_value=[])
    source = runner_reconciler._DockerWorkspaceSource("tcp://engine:2375")
    source._client = client

    listed = await source.list_labelled_workspaces()

    assert listed == []
    filters = client.containers.list.await_args.kwargs["filters"]
    assert filters["label"] == [
        "modulo.machine.id=deployment-a",
        "modulo.run.id",
    ]


async def test_docker_source_parses_labels_into_ages() -> None:
    client = MagicMock()
    listing = SimpleNamespace(
        Id="abc123",
        Labels={
            "modulo.run.id": "run-77",
            "modulo.machine.id": "deployment-a",
            "modulo.created_at": "1",
        },
    )
    client.containers.list = AsyncMock(return_value=[listing])
    source = runner_reconciler._DockerWorkspaceSource("tcp://engine:2375")
    source._client = client

    entries = await source.list_labelled_workspaces()

    assert len(entries) == 1
    assert entries[0].run_id == "run-77"
    assert entries[0].id == "abc123"
    assert entries[0].created_age_s > 0


def test_deployment_identity_env_then_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "machine-x")
    assert deployment_identity() == "machine-x"
    monkeypatch.delenv("MODULO_RUNNER_MACHINE_ID")
    assert deployment_identity()  # hostname fallback — non-empty
