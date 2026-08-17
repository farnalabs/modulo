"""Unit tests for DockerRuntimeProvider."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import ExecResult, WorkspaceSpec
from modulo.core.runtime_provider.docker import _DEFAULT_MEMORY_MB, DockerRuntimeProvider


class _FakeDockerError(Exception):
    """Stand-in for aiodocker.exceptions.DockerError used in error paths."""


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
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="python:3.13-slim",
        resource_limits={"memory_mb": 256},
    )


# ------------------------------------------------------------------
# supports
# ------------------------------------------------------------------


def test_supports_docker_hint(provider: DockerRuntimeProvider) -> None:
    profile = MagicMock(provider_hint="docker")
    assert provider.supports(profile) is True


def test_supports_image_ref(provider: DockerRuntimeProvider) -> None:
    profile = MagicMock(provider_hint="", image_ref="my-docker-image:latest")
    assert provider.supports(profile) is True


def test_supports_other_hint(provider: DockerRuntimeProvider) -> None:
    profile = MagicMock(provider_hint="e2b", image_ref="")
    assert provider.supports(profile) is False


def test_supports_docker_hint_case_insensitive(provider: DockerRuntimeProvider) -> None:
    profile = MagicMock(provider_hint="Docker")
    assert provider.supports(profile) is True


def test_supports_image_ref_case_insensitive(provider: DockerRuntimeProvider) -> None:
    profile = MagicMock(provider_hint="", image_ref="My-Docker-Image:latest")
    assert provider.supports(profile) is True


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
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        labels={"FOO": "bar", "MY_VAR": "value"},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert "FOO=bar" in config["Env"]
    assert "MY_VAR=value" in config["Env"]


async def test_create_workspace_default_image(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="",
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["Image"] == "python:3.13-slim"


async def test_create_workspace_strips_image_ref_whitespace(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="  python:3.13-slim  ",
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["Image"] == "python:3.13-slim"


async def test_create_workspace_invalid_memory_falls_back_to_default(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        resource_limits={"memory_mb": "lots"},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["Memory"] == _DEFAULT_MEMORY_MB * 1024 * 1024


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
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        resource_limits={"memory_mb": memory_mb},
    )
    await provider.create_workspace(spec)

    config = mock_docker_client.containers.create.call_args[1]["config"]
    assert config["HostConfig"]["Memory"] == expected_mb * 1024 * 1024


async def test_create_workspace_skips_env_control_characters(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
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
        await provider.create_workspace(
            WorkspaceSpec(
                environment_profile_id=uuid.uuid4(),
                organisation_id=uuid.uuid4(),
            )
        )

    assert not provider._workspaces


async def test_create_workspace_daemon_unreachable_raises_structured_error(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    """A ConnectionError from the Docker daemon surfaces as a structured 'daemon unreachable' error."""
    mock_docker_client.containers.create.side_effect = ConnectionError("Cannot connect to the Docker daemon")

    with pytest.raises(RuntimeError, match="Unable to reach the Docker daemon"):
        await provider.create_workspace(
            WorkspaceSpec(
                environment_profile_id=uuid.uuid4(),
                organisation_id=uuid.uuid4(),
            )
        )

    assert not provider._workspaces


# ------------------------------------------------------------------
# exec_command
# ------------------------------------------------------------------


async def test_exec_command(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )

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
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    stream = mock_docker_client.containers.get.return_value.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=[(None, b"boom"), None])
    mock_docker_client.containers.get.return_value.exec.return_value.inspect = AsyncMock(return_value={"ExitCode": 1})

    result = await provider.exec_command(ref, ["false"])

    assert result.exit_code == 1
    assert result.stderr == "boom"
    assert not result.stdout


async def test_exec_command_timeout_returns_timeout_result(
    provider: DockerRuntimeProvider,
    mock_docker_client: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )

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
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
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
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )

    await provider.destroy_workspace(ref)

    mock_container.stop.assert_awaited_once()
    assert ref not in provider._workspaces


async def test_destroy_workspace_unknown(
    provider: DockerRuntimeProvider,
) -> None:
    result = await provider.destroy_workspace("nonexistent")
    assert result is None


async def test_destroy_workspace_swallows_docker_error(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    mock_container.stop.side_effect = _FakeDockerError("no such container")

    with patch(
        "modulo.core.runtime_provider.docker.aiodocker.exceptions.DockerError",
        _FakeDockerError,
    ):
        await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


async def test_destroy_workspace_removes_ref_on_generic_error(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    mock_container.stop.side_effect = RuntimeError("connection reset")

    await provider.destroy_workspace(ref)

    assert ref not in provider._workspaces


# ------------------------------------------------------------------
# get_workspace_status
# ------------------------------------------------------------------


async def test_get_workspace_status_running(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )

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
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    mock_container.show.side_effect = RuntimeError("connection lost")

    status = await provider.get_workspace_status(ref)
    assert status == "unknown"


async def test_get_workspace_status_docker_error_returns_terminated(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
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
    mock_container: MagicMock,
) -> None:
    await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    assert len(provider._workspaces) == 2

    await provider.close()

    assert provider._client is None
    assert not provider._workspaces
    assert mock_container.stop.await_count == 2


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
        await provider.create_workspace(
            WorkspaceSpec(
                environment_profile_id=uuid.uuid4(),
                organisation_id=uuid.uuid4(),
            )
        )

    assert not provider._workspaces


async def test_exec_command_failure_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    stream = mock_container.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=RuntimeError("exec exploded"))

    with pytest.raises(RuntimeError, match="exec exploded"):
        await provider.exec_command(ref, ["echo", "hi"])

    assert "exec_command failed for container" in caplog.text


async def test_exec_command_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    mock_container.exec.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.exec_command(ref, ["echo", "hi"])


async def test_exec_command_stream_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    """Cancellation raised mid-stream must propagate through exec_command."""
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    stream = mock_container.exec.return_value.start.return_value
    stream.read_out = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await provider.exec_command(ref, ["echo", "hi"])


async def test_destroy_workspace_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    mock_container.stop.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.destroy_workspace(ref)


async def test_get_workspace_status_cancellation_propagates(
    provider: DockerRuntimeProvider,
    mock_container: MagicMock,
) -> None:
    ref = await provider.create_workspace(
        WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
        )
    )
    mock_container.show.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider.get_workspace_status(ref)
