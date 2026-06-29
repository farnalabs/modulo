"""Unit tests for DockerRuntimeProvider."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import ExecResult, WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider


@pytest.fixture()
def mock_container() -> MagicMock:
    container = MagicMock()
    container.id = "cntnr-12345"
    container.start = AsyncMock()
    container.stop = AsyncMock()
    container.delete = AsyncMock()

    exec_instance = MagicMock()
    exec_instance.start = AsyncMock(return_value=b"Hello, Docker!")

    container.exec = AsyncMock(return_value=exec_instance)
    container.show = AsyncMock(return_value={"State": {"Status": "running"}})
    return container


@pytest.fixture()
def mock_docker_client(mock_container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.ping = AsyncMock()
    client.close = AsyncMock()
    containers = MagicMock()
    containers.create = AsyncMock(return_value=mock_container)
    containers.get = AsyncMock(return_value=mock_container)
    client.containers = containers
    return client


@pytest.fixture()
def provider(mock_docker_client: MagicMock) -> DockerRuntimeProvider:
    with patch(
        "modulo.core.runtime_provider.docker.aiodocker.Docker",
        return_value=mock_docker_client,
    ):
        p = DockerRuntimeProvider(docker_host="unix:///var/run/docker.sock")
        # Force client to be our mock (bypass lazy init)
        p._client = mock_docker_client
        return p


@pytest.fixture()
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
    assert result.stderr == ""
    assert result.exit_code == 0


async def test_exec_command_unknown_ref(provider: DockerRuntimeProvider) -> None:
    with pytest.raises(ValueError, match="Unknown workspace"):
        await provider.exec_command("nonexistent", ["echo"])


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
    mock_container.delete.assert_awaited_once()
    assert ref not in provider._workspaces


async def test_destroy_workspace_unknown(
    provider: DockerRuntimeProvider,
) -> None:
    result = await provider.destroy_workspace("nonexistent")
    assert result is None


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
