"""Unit tests for DockerRuntimeProvider."""

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import ExecResult, WorkspaceSpec
from modulo.core.runtime_provider.docker import (
    _DEFAULT_HARDENING_MEMORY_MB,
    _DEFAULT_WORKSPACE_NETWORK,
    DockerRuntimeProvider,
)
from modulo.util import WorkspaceNetworkValidationError


class _FakeDockerError(Exception):
    """Stand-in for aiodocker.exceptions.DockerError used in error paths."""

    status: int | None = None


def _make_spec(**overrides: Any) -> WorkspaceSpec:
    """Build a minimal valid spec, overriding only the fields a test exercises."""
    fields: dict[str, Any] = {
        "environment_profile_id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
    }
    fields.update(overrides)
    return WorkspaceSpec(**fields)


@pytest.fixture(autouse=True)
def fake_docker_error() -> Iterator[type[_FakeDockerError]]:
    """Map aiodocker's DockerError onto the module stand-in for every test.

    The provider branches on ``aiodocker.exceptions.DockerError``; patching it
    once here keeps each error-path test to the behaviour it asserts instead of
    repeating the same ``patch()`` wrapper ten times.
    """
    with patch(
        "modulo.core.runtime_provider.docker.aiodocker.exceptions.DockerError",
        _FakeDockerError,
    ):
        yield _FakeDockerError


@pytest.fixture
def mock_container() -> MagicMock:
    container = MagicMock()
    container.id = "cntnr-12345"
    container.start = AsyncMock()
    container.stop = AsyncMock()
    container.delete = AsyncMock()

    exec_stream = MagicMock()
    exec_stream.read_out = AsyncMock(side_effect=[(b"Hello, Docker!", b""), None])

    exec_instance = MagicMock()
    exec_instance.start = AsyncMock(return_value=exec_stream)
    exec_instance.inspect = AsyncMock(return_value={"ExitCode": 0})

    container.exec = AsyncMock(return_value=exec_instance)
    container.show = AsyncMock(return_value={"State": {"Status": "running"}})
    return container


@pytest.fixture
def mock_docker_client(mock_container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.ping = AsyncMock()
    client.close = AsyncMock()
    client.images.pull = AsyncMock()
    containers = MagicMock()
    containers.create = AsyncMock(return_value=mock_container)
    containers.get = AsyncMock(return_value=mock_container)
    client.containers = containers
    return client


@pytest.fixture
def provider(mock_docker_client: MagicMock) -> DockerRuntimeProvider:
    with patch(
        "modulo.core.runtime_provider.docker.aiodocker.Docker",
        return_value=mock_docker_client,
    ):
        p = DockerRuntimeProvider(docker_host="unix:///var/run/docker.sock")
        # Force client to be our mock (bypass lazy init)
        p._client = mock_docker_client
        return p


@pytest.fixture
def spec() -> WorkspaceSpec:
    return _make_spec(
        image_ref="python:3.13-slim",
        resource_limits={"memory_mb": 256},
    )


# ------------------------------------------------------------------
# supports
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile_hint", "image_ref", "expected"),
    [
        ("docker", "", True),
        ("Docker", "", True),
        ("", "my-docker-image:latest", True),
        ("", "My-Docker-Image:latest", True),
        ("e2b", "", False),
    ],
    ids=["hint", "hint-case-insensitive", "image-ref", "image-ref-case-insensitive", "other-hint"],
)
def test_supports_matches_docker_hint_or_image_ref(
    provider: DockerRuntimeProvider,
    profile_hint: str,
    image_ref: str,
    expected: bool,
) -> None:
    profile = MagicMock(provider_hint=profile_hint, image_ref=image_ref)
    assert provider.supports(profile) is expected


# ------------------------------------------------------------------
# create_workspace
# ------------------------------------------------------------------


async def test_create_workspace(
    provider: DockerRuntimeProvider,
    spec: WorkspaceSpec,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(spec)

    mock_docker_client.containers.create.assert_called_once()
    call_kwargs = mock_docker_client.containers.create.call_args
    config = call_kwargs[1]["config"]
    assert config["Image"] == "python:3.13-slim"
    assert config["Cmd"] == ["sleep", "infinity"]
    assert config["HostConfig"]["AutoRemove"] is True
    assert config["HostConfig"]["Memory"] == 256 * 1024 * 1024

    mock_container.start.assert_awaited_once()
    assert isinstance(ref, str)
    assert provider._workspaces[ref] == "cntnr-12345"


async def test_create_workspace_with_env_labels(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = _make_spec(
        labels={"FOO": "bar", "MY_VAR": "value"},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert "FOO=bar" in config["Env"]
    assert "MY_VAR=value" in config["Env"]


async def test_create_workspace_repo_fields_are_ignored_no_clone(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """FAR-595 contract: Docker maps spec.labels -> container Env and has no
    clone semantics — first-class spec.repo_url/spec.repo_ref are ignored
    (code sync is the bundled runner image's job), and repo-ish labels keys
    are injected as plain env vars exactly like any other label.
    """
    spec = _make_spec(
        labels={"REPO_URL": "https://github.com/user/repo.git"},
        repo_url="https://github.com/user/repo.git",
        repo_ref="develop",
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    # labels still map to Env (env-var injection semantics)
    assert "REPO_URL=https://github.com/user/repo.git" in config["Env"]
    # first-class repo fields never leak into the container config
    for entry in config["Env"]:
        assert "git clone" not in entry
        assert entry != "develop"
    # and no exec/clone command was ever issued at provision time
    mock_docker_client.containers.create.assert_called_once()
    assert config["Cmd"] == ["sleep", "infinity"]


async def test_create_workspace_default_image(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = _make_spec(
        image_ref="",
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["Image"] == "python:3.13-slim"


async def test_create_workspace_strips_image_ref_whitespace(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = _make_spec(
        image_ref="  python:3.13-slim  ",
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["Image"] == "python:3.13-slim"


async def test_create_workspace_invalid_memory_falls_back_to_default(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = _make_spec(
        resource_limits={"memory_mb": "lots"},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["Memory"] == _DEFAULT_HARDENING_MEMORY_MB * 1024 * 1024


@pytest.mark.parametrize(
    ("memory_mb", "expected_mb"),
    [
        (1, 4),
        (131072, 131072),
        (1000000, 131072),
    ],
)
async def test_create_workspace_clamps_memory(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    memory_mb: int,
    expected_mb: int,
) -> None:
    spec = _make_spec(
        resource_limits={"memory_mb": memory_mb},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["Memory"] == expected_mb * 1024 * 1024


async def test_create_workspace_skips_env_control_characters(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = _make_spec(
        labels={"OK": "fine", "BAD": "bad\nvalue", "CARRIAGE": "x\ry", "NUL": "z\0w"},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["Env"] == ["OK=fine"]


async def test_create_workspace_propagates_container_failure(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    mock_docker_client.containers.create.side_effect = RuntimeError("docker down")

    with pytest.raises(RuntimeError, match="docker down"):
        await provider.create_workspace(_make_spec())

    assert not provider._workspaces


async def test_create_workspace_daemon_unreachable_raises_structured_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """A ConnectionError from the Docker daemon surfaces as a structured 'daemon unreachable' error."""
    mock_docker_client.containers.create.side_effect = ConnectionError("Cannot connect to the Docker daemon")

    with pytest.raises(RuntimeError, match="Unable to reach the Docker daemon"):
        await provider.create_workspace(_make_spec())

    assert not provider._workspaces


async def test_create_workspace_uses_dedicated_workspace_network_by_default(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """The default is the dedicated workspace bridge, never the compose network."""
    await provider.create_workspace(_make_spec())

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["NetworkMode"] == _DEFAULT_WORKSPACE_NETWORK


async def test_create_workspace_honours_spec_workspace_network(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """A profile-selected, deployment-owned bridge network overrides the default."""
    await provider.create_workspace(_make_spec(workspace_network="custom-workspace-net"))

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["NetworkMode"] == "custom-workspace-net"


async def test_create_workspace_egress_none_disables_networking(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """An explicit ``egress_policy="none"`` profile runs with no network at all."""
    await provider.create_workspace(_make_spec(egress_policy="none", workspace_network="custom-workspace-net"))

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["NetworkMode"] == "none"


@pytest.mark.parametrize("dangerous_network", ["host", "bridge", "container:abc123"])
async def test_create_workspace_rejects_dangerous_network_mode(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    dangerous_network: str,
) -> None:
    """FAR-1020: an isolation-defeating network is rejected at the provider boundary."""
    with pytest.raises(WorkspaceNetworkValidationError):
        await provider.create_workspace(_make_spec(workspace_network=dangerous_network))

    mock_docker_client.containers.create.assert_not_called()
    assert not provider._workspaces


async def test_create_workspace_pulls_image_before_create(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """Provisioning pulls the image first so an absent local image is not fatal."""
    await provider.create_workspace(_make_spec(image_ref="python:3.13-slim"))

    mock_docker_client.images.pull.assert_awaited_once_with("python:3.13-slim")
    mock_docker_client.containers.create.assert_called_once()


async def test_create_workspace_swallows_image_pull_failure(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """The pull is best-effort: a failure falls through to create (which reports it)."""
    mock_docker_client.images.pull.side_effect = RuntimeError("registry unreachable")

    ref = await provider.create_workspace(_make_spec())

    assert provider._workspaces[ref] == "cntnr-12345"


async def test_create_workspace_propagates_image_pull_cancellation(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """Cancellation must not be swallowed by the best-effort pull."""
    mock_docker_client.images.pull.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.create_workspace(_make_spec())

    assert not provider._workspaces


# ------------------------------------------------------------------
# exec_command
# ------------------------------------------------------------------


async def test_exec_command(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())

    result = await provider.exec_command(ref, ["echo", "hi"])

    mock_docker_client.containers.get.assert_called_with("cntnr-12345")
    mock_container.exec.assert_awaited_once_with(cmd=["echo", "hi"])
    assert isinstance(result, ExecResult)
    assert result.stdout == "Hello, Docker!"
    assert not result.stderr
    assert result.exit_code == 0
    assert result.duration_ms is not None
    assert result.duration_ms >= 0


async def test_exec_command_unknown_ref(provider: DockerRuntimeProvider) -> None:
    with pytest.raises(ValueError, match="Unknown workspace"):
        await provider.exec_command("nonexistent", ["echo"])


async def test_exec_command_captures_stderr_and_exit_code(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    stream = mock_docker_client.containers.get.return_value.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=[(None, b"boom"), None])
    mock_docker_client.containers.get.return_value.exec.return_value.inspect = AsyncMock(return_value={"ExitCode": 1})

    result = await provider.exec_command(ref, ["false"])

    assert result.exit_code == 1
    assert result.stderr == "boom"
    assert not result.stdout


async def test_exec_command_reports_missing_exit_code_as_minus_one(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    """An inspect payload without ``ExitCode`` must not fabricate a success."""
    ref = await provider.create_workspace(_make_spec())
    mock_container.exec.return_value.inspect = AsyncMock(return_value={})

    result = await provider.exec_command(ref, ["echo", "hi"])

    assert result.exit_code == -1
    assert result.stdout == "Hello, Docker!"


async def test_exec_command_timeout_returns_timeout_result(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())

    async def _hang() -> None:
        await asyncio.Event().wait()

    stream = mock_docker_client.containers.get.return_value.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=_hang)

    result = await provider.exec_command(ref, ["sleep", "100"], cmd_timeout=0)

    assert result.exit_code == -1
    assert result.stderr == "Command timed out"
    assert not result.stdout
    assert result.duration_ms is not None
    assert result.duration_ms >= 0


async def test_exec_command_decodes_invalid_utf8_with_replacement(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    stream = mock_docker_client.containers.get.return_value.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=[(b"\xff\xfe\xff", None), None])

    result = await provider.exec_command(ref, ["cat", "binary"])

    assert result.stdout == "\ufffd\ufffd\ufffd"


# ------------------------------------------------------------------
# destroy_workspace
# ------------------------------------------------------------------


async def test_destroy_workspace(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())

    await provider.destroy_workspace(ref)

    mock_container.stop.assert_awaited_once()
    mock_container.delete.assert_awaited_once_with(force=True)
    assert ref not in provider._workspaces


async def test_destroy_workspace_unknown(
    provider: DockerRuntimeProvider,
) -> None:
    await provider.destroy_workspace("nonexistent")

    assert provider._workspaces == {}


async def test_destroy_workspace_swallows_delete_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    delete_error = _FakeDockerError("removal already in progress")
    delete_error.status = 409
    gone_error = _FakeDockerError("gone")
    gone_error.status = 404
    mock_container.delete.side_effect = delete_error
    # First get() resolves the container handle; the removal-confirmation poll
    # then sees it already gone.
    mock_docker_client.containers.get.side_effect = [mock_container, gone_error]

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_swallows_docker_error(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.stop.side_effect = _FakeDockerError("no such container")

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_removes_ref_on_generic_error(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.stop.side_effect = RuntimeError("connection reset")

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_container_already_gone(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    gone_error = _FakeDockerError("no such container")
    gone_error.status = 404
    mock_docker_client.containers.get.side_effect = gone_error

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_resolve_non_404_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    daemon_error = _FakeDockerError("daemon unavailable")
    daemon_error.status = 500
    mock_docker_client.containers.get.side_effect = daemon_error

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_resolve_generic_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_docker_client.containers.get.side_effect = RuntimeError("connection reset")

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_propagates_resolve_cancellation(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_docker_client.containers.get.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await provider.destroy_workspace(ref)


async def test_destroy_workspace_propagates_stop_cancellation(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.stop.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await provider.destroy_workspace(ref)


async def test_destroy_workspace_propagates_delete_cancellation(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.delete.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await provider.destroy_workspace(ref)


async def test_destroy_workspace_delete_non_404_error_confirms_removal(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    daemon_error = _FakeDockerError("server error")
    daemon_error.status = 500
    gone_error = _FakeDockerError("gone")
    gone_error.status = 404
    mock_container.delete.side_effect = daemon_error
    mock_docker_client.containers.get.side_effect = [mock_container, gone_error]

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_delete_generic_error_confirms_removal(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    gone_error = _FakeDockerError("gone")
    gone_error.status = 404
    mock_container.delete.side_effect = RuntimeError("connection reset")
    mock_docker_client.containers.get.side_effect = [mock_container, gone_error]

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_await_container_removed_polls_until_gone(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    gone_error = _FakeDockerError("gone")
    gone_error.status = 404
    mock_docker_client.containers.get.side_effect = [mock_container, mock_container, gone_error]

    removed = await provider._await_container_removed(mock_docker_client, "cntnr-12345")

    assert removed is True


async def test_await_container_removed_propagates_cancellation(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    mock_docker_client.containers.get.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await provider._await_container_removed(mock_docker_client, "cntnr-12345")


async def test_await_container_removed_returns_false_on_non_404_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    daemon_error = _FakeDockerError("server error")
    daemon_error.status = 500
    mock_docker_client.containers.get.side_effect = daemon_error

    removed = await provider._await_container_removed(mock_docker_client, "cntnr-12345")

    assert removed is False


async def test_await_container_removed_returns_false_on_generic_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    mock_docker_client.containers.get.side_effect = RuntimeError("connection reset")

    removed = await provider._await_container_removed(mock_docker_client, "cntnr-12345")

    assert removed is False


async def test_await_container_removed_returns_false_when_still_present(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    mock_docker_client.containers.get.return_value = mock_container

    with patch("modulo.core.runtime_provider.docker._REMOVE_CONFIRM_TIMEOUT_S", 0.0):
        removed = await provider._await_container_removed(mock_docker_client, "cntnr-12345")

    assert removed is False


async def test_destroy_workspace_warns_when_container_survives(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When removal cannot be confirmed, destroy still drops the ref and warns."""
    ref = await provider.create_workspace(_make_spec())
    delete_error = _FakeDockerError("removal already in progress")
    delete_error.status = 409
    mock_container.delete.side_effect = delete_error
    mock_docker_client.containers.get.return_value = mock_container
    monkeypatch.setattr("modulo.core.runtime_provider.docker._REMOVE_CONFIRM_TIMEOUT_S", 0.0)

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces
    assert "still present after destroy" in caplog.text


# ------------------------------------------------------------------
# get_workspace_status
# ------------------------------------------------------------------


async def test_get_workspace_status_running(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())

    status = await provider.get_workspace_status(ref)

    assert status == "running"
    mock_container.show.assert_awaited_once()


async def test_get_workspace_status_terminated(
    provider: DockerRuntimeProvider,
) -> None:
    status = await provider.get_workspace_status("nonexistent")
    assert status == "terminated"


async def test_get_workspace_status_fallback_on_error(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.show.side_effect = RuntimeError("connection lost")

    status = await provider.get_workspace_status(ref)
    assert status == "unknown"


async def test_get_workspace_status_docker_error_returns_terminated(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.show.side_effect = _FakeDockerError("no such container")

    with patch(
        "modulo.core.runtime_provider.docker.aiodocker.exceptions.DockerError",
        _FakeDockerError,
    ):
        status = await provider.get_workspace_status(ref)

    assert status == "terminated"


# ------------------------------------------------------------------
# close
# ------------------------------------------------------------------


async def test_close(provider: DockerRuntimeProvider) -> None:
    assert provider._client is not None
    await provider.close()
    assert provider._client is None


async def test_close_idempotent(provider: DockerRuntimeProvider) -> None:
    await provider.close()
    await provider.close()
    assert provider._client is None


async def test_close_destroys_all_workspaces(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    second_container = MagicMock()
    second_container.id = "cntnr-67890"
    second_container.start = AsyncMock()
    second_container.stop = AsyncMock()
    second_container.delete = AsyncMock()
    containers_by_id = {mock_container.id: mock_container, second_container.id: second_container}
    mock_docker_client.containers.create.side_effect = [mock_container, second_container]
    mock_docker_client.containers.get.side_effect = lambda container_id: containers_by_id[container_id]

    await provider.create_workspace(_make_spec())
    await provider.create_workspace(_make_spec())
    assert len(set(provider._workspaces.values())) == 2

    await provider.close()

    assert provider._client is None
    assert not provider._workspaces
    # Both distinct containers are stopped and force-deleted, not just tracked.
    mock_container.stop.assert_awaited_once()
    second_container.stop.assert_awaited_once()
    mock_container.delete.assert_awaited_once_with(force=True)
    second_container.delete.assert_awaited_once_with(force=True)


async def test_get_client_lazily_initializes_once(mock_docker_client: MagicMock) -> None:
    with patch(
        "modulo.core.runtime_provider.docker.aiodocker.Docker",
        return_value=mock_docker_client,
    ) as docker_cls:
        p = DockerRuntimeProvider(docker_host="unix:///var/run/docker.sock")
        assert p._client is None

        first = await p._get_client()
        second = await p._get_client()

    assert first is mock_docker_client
    assert second is mock_docker_client
    assert p._client is mock_docker_client
    docker_cls.assert_called_once_with(url="unix:///var/run/docker.sock")


# ------------------------------------------------------------------
# Cancellation propagation
# ------------------------------------------------------------------


async def test_create_workspace_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    mock_docker_client.containers.create.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.create_workspace(_make_spec())

    assert not provider._workspaces


async def test_exec_command_failure_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    stream = mock_container.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=RuntimeError("exec exploded"))

    with pytest.raises(RuntimeError, match="exec exploded"):
        await provider.exec_command(ref, ["echo", "hi"])

    assert "exec_command failed for container" in caplog.text


async def test_exec_command_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.exec.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.exec_command(ref, ["echo", "hi"])


async def test_exec_command_read_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    """Cancellation raised while reading exec output must propagate through exec_command."""
    ref = await provider.create_workspace(_make_spec())
    stream = mock_container.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await provider.exec_command(ref, ["echo", "hi"])


async def test_get_workspace_status_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(_make_spec())
    mock_container.show.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.get_workspace_status(ref)


# ------------------------------------------------------------------
# FAR-1036: non-root user for ALL images (default)
# ------------------------------------------------------------------


@pytest.mark.parametrize("image_ref", ["python:3.13-slim", "modulo-runner:opencode", ""])
async def test_create_workspace_stamps_non_root_user_for_every_image(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    image_ref: str,
) -> None:
    """FAR-1036: every image — including the default and modulo-runner — runs non-root."""
    spec = _make_spec(
        image_ref=image_ref,
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["User"] == "1001:1001"


async def test_create_workspace_root_user_opt_in(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1036: allow_root_user=True skips the non-root stamp and logs a warning."""
    spec = _make_spec(
        image_ref="custom-base:latest",
        allow_root_user=True,
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert "User" not in config
    assert "running as root (allow_root_user=True)" in caplog.text


# ------------------------------------------------------------------
# FAR-1037: explicit seccomp + AppArmor profiles
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("required_entry", "description"),
    [
        ("no-new-privileges:true", "no-new-privileges"),
        ("seccomp=", "seccomp profile"),
        ("apparmor=", "AppArmor profile"),
    ],
)
async def test_create_workspace_applies_hardening_security_options(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    required_entry: str,
    description: str,
) -> None:
    """FAR-1037: every workspace must carry no-new-privileges, seccomp and AppArmor."""
    spec = _make_spec()
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    security_opt = config["HostConfig"]["SecurityOpt"]
    if required_entry.endswith("="):
        assert any(entry.startswith(required_entry) for entry in security_opt), (
            f"Expected a {description} entry in SecurityOpt, got: {security_opt}"
        )
    else:
        assert required_entry in security_opt, f"Expected {description} in SecurityOpt, got: {security_opt}"


# ------------------------------------------------------------------
# FAR-1493: reserved identity labels are provider-authoritative
# ------------------------------------------------------------------


async def test_create_workspace_reserved_identity_labels_cannot_be_restamped(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """FAR-1493: operator metadata must NOT re-stamp reserved identity labels.

    The reserved keys are stamped AFTER the operator metadata (assignment,
    not ``setdefault``) so identity stays this provider's own fact. A
    caller-supplied ``modulo.machine.id`` would hide the container from THIS
    deployment's orphan sweep (the reconciler lists by that exact label —
    a foreign value makes the container invisible to its own deployment's
    reclamation); a caller-supplied ``modulo.created_at`` would defeat the
    5-minute grace window and the 24-hour max-lifetime backstop (a backdated
    stamp ages the container past reclaim-on-sight; a future stamp makes it
    look age-new forever). Both are permanent orphan leaks.

    Non-reserved attribution keys still flow through unchanged.
    """
    spec = _make_spec(
        workspace_metadata={
            "modulo.machine.id": "evil-deployment",
            "modulo.created_at": "1",
            "modulo.run.id": "7d9f0000-0000-4000-8000-000000000001",
            "custom.key": "custom-value",
        },
    )
    await provider.create_workspace(spec)

    labels = mock_docker_client.containers.create.call_args[1]["config"]["Labels"]
    # The identity keys win outright — assignment, not setdefault.
    assert labels["modulo.machine.id"] == provider._deployment_identity()
    # A fresh creation stamp, never the caller's backdated value.
    assert int(labels["modulo.created_at"]) > 1
    # Non-reserved keys still round-trip exactly as supplied.
    assert labels["modulo.run.id"] == "7d9f0000-0000-4000-8000-000000000001"
    assert labels["custom.key"] == "custom-value"


def test_build_workspace_labels_identity_is_authoritative(
    provider: DockerRuntimeProvider,
) -> None:
    """FAR-1493 (unit): ``_build_workspace_labels`` itself is leak-proof.

    Direct probe of the label mapper: even when the whole reserved set is
    pre-populated in ``workspace_metadata``, the returned mapping carries the
    provider's own deployment identity and a current (non-backdated) epoch.
    """
    labels = provider._build_workspace_labels(
        _make_spec(
            workspace_metadata={
                "modulo.machine.id": "someone-else",
                "modulo.created_at": "1",
            },
        )
    )

    assert labels["modulo.machine.id"] == provider._deployment_identity()
    created = int(labels["modulo.created_at"])
    assert created > 1
    assert created <= int(time.time()) + 60
