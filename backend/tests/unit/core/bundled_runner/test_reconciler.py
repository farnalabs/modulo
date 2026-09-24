"""Unit tests for the Docker workspace-orphan reconciler sweep (FAR-590 D4).

Covers the committed semantics: grace period, log-only soak default,
destroy-mode orphan removal, active-run spare + destroy-path false-positive
re-check, the 24h max-lifetime backstop (applies regardless of run state),
fail-safe abort on cross-reference failure, and machine-scoped container
filters.

FAR-1201 follow-up: the engine-less skip — ``docker_endpoint_skip_reason``
and the sweep's early return when NO Docker endpoint is resolvable.
"""

import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.bundled_runner import runner_reconciler
from modulo.core.bundled_runner.runner_reconciler import (
    ReconcilerSweepError,
    _LabelledContainer,
    deployment_identity,
    docker_endpoint_skip_reason,
    reconcile_runner_workspaces,
)


@pytest.fixture(autouse=True)
def _configured_docker_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1201: the sweep skips when NO endpoint is resolvable — force one.

    The existing behaviour tests below exercise the sweep machinery with a
    fake ``_DockerWorkspaceSource``; without this fixture they would take
    the engine-less skip path on any host lacking a Docker socket/context
    (CI containers, engine-less dev boxes) and fail non-deterministically.
    Skip-path tests delenv this and patch the filesystem probes explicitly.
    """
    monkeypatch.setenv("MODULO_DOCKER_HOST", "tcp://docker-socket-proxy:2375")


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
    # Shipped compose-internal endpoint (FAR-1038-exempt).  The test mocks the
    # client, so no real connection occurs; TLS validation is covered separately.
    source = runner_reconciler._DockerWorkspaceSource("tcp://docker-socket-proxy:2375")
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
    # Shipped compose-internal endpoint (FAR-1038-exempt).
    source = runner_reconciler._DockerWorkspaceSource("tcp://docker-socket-proxy:2375")
    source._client = client

    entries = await source.list_labelled_workspaces()

    assert len(entries) == 1
    assert entries[0].run_id == "run-77"
    assert entries[0].id == "abc123"
    assert entries[0].created_age_s > 0


def test_docker_source_rejects_remote_endpoint_without_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1038: the reconciler shares the provider's TLS gate — a remote
    cleartext endpoint is rejected at source construction, not silently
    connected."""
    monkeypatch.delenv("DOCKER_TLS_VERIFY", raising=False)
    monkeypatch.delenv("DOCKER_CERT_PATH", raising=False)
    monkeypatch.delenv("MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="requires TLS"):
        runner_reconciler._DockerWorkspaceSource("tcp://engine:2375")


def test_docker_source_accepts_remote_endpoint_with_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same remote endpoint is accepted once TLS is configured."""
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/certs")
    source = runner_reconciler._DockerWorkspaceSource("tcp://engine:2375")
    assert source._docker_host == "tcp://engine:2375"


def test_deployment_identity_env_then_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "machine-x")
    assert deployment_identity() == "machine-x"
    monkeypatch.delenv("MODULO_RUNNER_MACHINE_ID")
    assert deployment_identity()  # hostname fallback — non-empty


# ---------------------------------------------------------------------------
# FAR-1201: engine-less skip — docker_endpoint_skip_reason + sweep early return
# ---------------------------------------------------------------------------


def _force_no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a deployment with NO resolvable Docker endpoint.

    Clears the documented env chain and patches both filesystem probes so
    the result does not depend on the test host having (or lacking) a
    Docker socket/context.
    """
    monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(runner_reconciler, "_context_endpoint_configured", lambda: False)
    monkeypatch.setattr(runner_reconciler, "_default_docker_socket_present", lambda: False)


def _isolate_from_context_and_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the non-env signals so only the env chain can decide."""
    monkeypatch.setattr(runner_reconciler, "_context_endpoint_configured", lambda: False)
    monkeypatch.setattr(runner_reconciler, "_default_docker_socket_present", lambda: False)


def test_skip_reason_none_when_env_endpoint_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODULO_DOCKER_HOST alone (autouse fixture) makes the sweep applicable —
    even with every other signal neutralised. A configured-but-unreachable
    endpoint therefore NEVER skips: the predicate never probes reachability."""
    _isolate_from_context_and_socket(monkeypatch)

    assert docker_endpoint_skip_reason() is None


def test_skip_reason_present_when_no_endpoint_resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env, no context, no socket → explicit not-applicable reason."""
    _force_no_endpoint(monkeypatch)

    reason = docker_endpoint_skip_reason()

    assert reason is not None
    assert "no Docker endpoint configured" in reason
    assert "MODULO_DOCKER_HOST" in reason  # names the remediation knob


def test_skip_reason_none_when_docker_host_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCKER_HOST (the documented step-3 fallback) also counts as configured."""
    monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    _isolate_from_context_and_socket(monkeypatch)

    assert docker_endpoint_skip_reason() is None


def test_skip_reason_none_when_docker_context_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A selected Docker context is an endpoint (aiodocker resolves it)."""
    _force_no_endpoint(monkeypatch)
    monkeypatch.setattr(runner_reconciler, "_context_endpoint_configured", lambda: True)

    assert docker_endpoint_skip_reason() is None


def test_skip_reason_none_when_local_socket_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw-socket operator override (step 4) is applicable — never skipped."""
    _force_no_endpoint(monkeypatch)
    monkeypatch.setattr(runner_reconciler, "_default_docker_socket_present", lambda: True)

    assert docker_endpoint_skip_reason() is None


async def test_sweep_skips_with_explicit_reason_when_no_endpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Engine-less deployment: the sweep returns a skip envelope, never
    constructs an engine client, and logs the reason (never a silent swallow)."""
    _force_no_endpoint(monkeypatch)
    source_factory = MagicMock(
        side_effect=AssertionError("_DockerWorkspaceSource must not be constructed when skipping")
    )
    monkeypatch.setattr(runner_reconciler, "_DockerWorkspaceSource", source_factory)

    with caplog.at_level(logging.INFO):
        result = await reconcile_runner_workspaces(_engine_with_active_runs([]))

    assert result["scanned"] == 0
    assert result["orphans_destroyed"] == 0
    assert "no Docker endpoint configured" in result["skipped"]
    assert "runner.reconciler.skipped" in caplog.text
    source_factory.assert_not_called()


def test_local_socket_paths_mirror_aiodocker_search_list() -> None:
    """The socket probe mirrors aiodocker's ``_sock_search_paths``.

    Path objects are compared (not ``str``) so the assertion holds on
    Windows hosts too, where ``str(Path("/run/..."))`` normalises to
    backslashes.
    """
    paths = runner_reconciler._local_docker_socket_paths()
    assert Path("/run/docker.sock") in paths
    assert Path("/var/run/docker.sock") in paths


def test_context_configured_from_docker_config_current_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``currentContext`` in config.json counts as a configured endpoint."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    config = tmp_path / "config.json"
    config.write_text('{"currentContext": "remote-engine"}', encoding="utf-8")
    monkeypatch.setattr(runner_reconciler, "_docker_config_path", lambda: config)

    assert runner_reconciler._context_endpoint_configured() is True


def test_context_not_configured_when_default_or_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``"default"`` currentContext / missing config file = no context endpoint."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    missing = tmp_path / "no-such-config.json"
    monkeypatch.setattr(runner_reconciler, "_docker_config_path", lambda: missing)
    assert runner_reconciler._context_endpoint_configured() is False

    config = tmp_path / "config.json"
    config.write_text('{"currentContext": "default"}', encoding="utf-8")
    monkeypatch.setattr(runner_reconciler, "_docker_config_path", lambda: config)
    assert runner_reconciler._context_endpoint_configured() is False


def test_docker_context_env_default_suppresses_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DOCKER_CONTEXT=default means "no context" even when config.json
    selects one — mirrors aiodocker's env-over-config precedence."""
    monkeypatch.setenv("DOCKER_CONTEXT", "default")
    config = tmp_path / "config.json"
    config.write_text('{"currentContext": "remote-engine"}', encoding="utf-8")
    monkeypatch.setattr(runner_reconciler, "_docker_config_path", lambda: config)

    assert runner_reconciler._context_endpoint_configured() is False


def test_docker_context_env_set_selects_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default DOCKER_CONTEXT env value is an endpoint signal."""
    monkeypatch.setenv("DOCKER_CONTEXT", "production")

    assert runner_reconciler._context_endpoint_configured() is True


def test_malformed_docker_config_counts_as_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A malformed config.json makes aiodocker RAISE at construction — that
    error must surface as a failed sweep, never be skipped away."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    config = tmp_path / "config.json"
    config.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(runner_reconciler, "_docker_config_path", lambda: config)

    assert runner_reconciler._context_endpoint_configured() is True


def test_non_string_current_context_counts_as_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-string currentContext (e.g. a number) makes aiodocker RAISE
    (AttributeError on .encode) at construction — must surface, not skip."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    config = tmp_path / "config.json"
    config.write_text('{"currentContext": 5}', encoding="utf-8")
    monkeypatch.setattr(runner_reconciler, "_docker_config_path", lambda: config)

    assert runner_reconciler._context_endpoint_configured() is True
