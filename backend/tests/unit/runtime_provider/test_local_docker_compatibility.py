"""Contract tests for the Local Docker compatibility adapter."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorQuery

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
from modulo.connectors.shell import ShellConnector
from modulo.core.runtime_provider import ExecResult, WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider
from modulo.core.runtime_provider.local_docker import LocalDockerRuntimeProvider


@pytest.fixture()
def canonical_provider() -> MagicMock:
    provider = MagicMock(spec=DockerRuntimeProvider)
    provider.create_workspace = AsyncMock(return_value="workspace-ref")
    provider.exec_command = AsyncMock(return_value=ExecResult(0, "ok", "", 7))
    provider.destroy_workspace = AsyncMock()
    provider.get_workspace_status = AsyncMock(return_value="running")
    provider.close = AsyncMock()
    provider.supports.return_value = True
    provider._get_container_id.return_value = "container-id"
    return provider


def test_constructor_preserves_legacy_timeout_semantics() -> None:
    with patch("modulo.core.runtime_provider.local_docker.DockerRuntimeProvider") as provider_class:
        LocalDockerRuntimeProvider(
            docker_host="tcp://docker:2375",
            default_image="python:test",
            timeout_seconds=45,
        )

    provider_class.assert_called_once_with(
        docker_host="tcp://docker:2375",
        default_image="python:test",
        create_timeout=45,
    )


async def test_create_workspace_preserves_mapping_return(canonical_provider: MagicMock) -> None:
    adapter = LocalDockerRuntimeProvider(provider=canonical_provider)
    profile = SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="python:test",
        capabilities_json=["shell"],
        config_json={"memory_mb": 128},
    )

    workspace = await adapter.create_workspace(profile)

    assert workspace == {"ref": "workspace-ref", "container_id": "container-id"}
    spec = canonical_provider.create_workspace.await_args.args[0]
    assert isinstance(spec, WorkspaceSpec)
    assert spec.environment_profile_id == profile.id
    assert spec.organisation_id == profile.organisation_id
    assert spec.image_ref == "python:test"


async def test_execute_command_preserves_dictionary_result(canonical_provider: MagicMock) -> None:
    adapter = LocalDockerRuntimeProvider(provider=canonical_provider)

    result = await adapter.execute_command(
        {"ref": "workspace-ref"},
        "python -m pytest",
        cwd="/workspace",
        env={"CI": "true"},
        timeout_seconds=12,
    )

    assert result == {"stdout": "ok", "stderr": "", "exit_code": 0, "duration_ms": 7}
    canonical_provider.exec_command.assert_awaited_once_with(
        "workspace-ref",
        ["sh", "-c", "cd /workspace && env CI=true python -m pytest"],
        timeout=12,
    )


async def test_health_and_file_helpers_delegate_to_canonical_provider(canonical_provider: MagicMock) -> None:
    adapter = LocalDockerRuntimeProvider(provider=canonical_provider)
    canonical_provider.exec_command.side_effect = [
        ExecResult(0, "", "", 1),
        ExecResult(0, "contents", "", 1),
        ExecResult(0, ".\n..\na.txt\nb.txt\n", "", 1),
    ]

    assert await adapter.workspace_health("workspace-ref") is True
    await adapter.write_file("workspace-ref", "/workspace/a.txt", "contents")
    assert await adapter.read_file("workspace-ref", "/workspace/a.txt") == "contents"
    assert await adapter.list_files("workspace-ref", "/workspace") == ["a.txt", "b.txt"]


async def test_workspace_health_reports_unsupported_workspace(canonical_provider: MagicMock) -> None:
    canonical_provider.get_workspace_status.return_value = "terminated"
    adapter = LocalDockerRuntimeProvider(provider=canonical_provider)

    assert await adapter.workspace_health("missing") is False


async def test_shell_connector_operates_against_local_docker_adapter(canonical_provider: MagicMock) -> None:
    canonical_provider.exec_command.side_effect = [
        ExecResult(0, "hello\n", "", 3),
        ExecResult(0, "file contents", "", 2),
    ]
    adapter = LocalDockerRuntimeProvider(provider=canonical_provider)
    connector = ShellConnector(
        runtime_provider=adapter,
        allowed_commands=["echo"],
        workspace_lease_id=uuid.uuid4(),
    )

    command_result = await connector.write(
        ConnectorPayload(
            resource="command",
            data={"command": "echo hello", "provider_ref": "workspace-ref"},
        )
    )
    file_result = await connector.query(
        ConnectorQuery(
            resource="file",
            filters={"path": "/workspace/a.txt", "provider_ref": "workspace-ref"},
        )
    )

    assert command_result["stdout"] == "hello\n"
    assert command_result["exit_code"] == 0
    assert file_result.records == [{"path": "/workspace/a.txt", "content": "file contents"}]
