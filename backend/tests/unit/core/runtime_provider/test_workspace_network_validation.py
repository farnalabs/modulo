"""Unit tests for workspace_network validation (FAR-1020).

Structural tests asserting that dangerous Docker network modes
(host, container:*, bridge, none, default) can never reach the
container's HostConfig.NetworkMode through any code path.
"""

import uuid
from types import SimpleNamespace

import pytest

from modulo.util import WorkspaceNetworkValidationError, validate_workspace_network

# ---------------------------------------------------------------------------
# validate_workspace_network — the single source of truth
# ---------------------------------------------------------------------------


class TestValidateWorkspaceNetwork:
    """Direct tests of the validation function."""

    def test_none_passes(self) -> None:
        assert validate_workspace_network(None) is None

    def test_empty_string_passes(self) -> None:
        assert validate_workspace_network("") is None

    def test_whitespace_only_passes(self) -> None:
        assert validate_workspace_network("   ") is None

    def test_default_network_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="default"):
            validate_workspace_network("default")

    def test_host_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="host"):
            validate_workspace_network("host")

    def test_host_case_insensitive_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="Host"):
            validate_workspace_network("Host")

    def test_bridge_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="bridge"):
            validate_workspace_network("bridge")

    def test_none_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="none"):
            validate_workspace_network("none")

    def test_container_sharing_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="container:"):
            validate_workspace_network("container:abc123")

    def test_container_sharing_case_insensitive_rejected(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="container:"):
            validate_workspace_network("Container:abc123")

    def test_valid_bridge_name_accepted(self) -> None:
        assert validate_workspace_network("modulo-runner-workspace") == "modulo-runner-workspace"

    def test_valid_name_with_dots_accepted(self) -> None:
        assert validate_workspace_network("my.network.name") == "my.network.name"

    def test_valid_name_with_dashes_accepted(self) -> None:
        assert validate_workspace_network("my-network") == "my-network"

    def test_valid_name_with_underscores_accepted(self) -> None:
        assert validate_workspace_network("my_network") == "my_network"

    def test_valid_name_stripped(self) -> None:
        assert validate_workspace_network("  modulo-runner-workspace  ") == "modulo-runner-workspace"

    def test_rejects_names_with_spaces(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError):
            validate_workspace_network("my network")

    def test_rejects_names_with_slashes(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError):
            validate_workspace_network("network/slash")

    def test_rejects_names_starting_with_dash(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError):
            validate_workspace_network("-bad-name")

    def test_rejects_names_starting_with_dot(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError):
            validate_workspace_network(".hidden")

    def test_error_carries_value(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError, match="host") as exc_info:
            validate_workspace_network("host")
        assert exc_info.value.value == "host"

    def test_rejects_host_with_port_suffix(self) -> None:
        with pytest.raises(WorkspaceNetworkValidationError):
            validate_workspace_network("host:8080")


# ---------------------------------------------------------------------------
# _workspace_spec_for_dispatch — dispatch-time defence in depth
# ---------------------------------------------------------------------------


def _profile(provider_type: str, **kwargs: object) -> SimpleNamespace:
    """Minimal profile stub for dispatch tests."""
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "name": "Test Profile",
        "provider_type": provider_type,
        "image_ref": "modulo-runner:opencode@sha256:" + "a" * 64,
        "persistence_policy": "ephemeral",
        "network_policy": "outbound",
        "capabilities_json": [],
        "config_json": {"timeout_seconds": 1800, "memory_mb": 1024, "workspace_network": "modulo-runner-workspace"},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_dispatch_rejects_host_network_mode() -> None:
    """Structural test: _workspace_spec_for_dispatch must reject host."""
    from modulo.core.bundled_runner.runner_dispatch import _workspace_spec_for_dispatch

    profile = _profile("runner_docker", config_json={"workspace_network": "host"})
    with pytest.raises(WorkspaceNetworkValidationError, match="host"):
        _workspace_spec_for_dispatch(
            profile,
            org_id=uuid.uuid4(),
            run_id="run-123",
            node_id="node-9",
            run_uuid=uuid.uuid4(),
        )


def test_dispatch_rejects_container_sharing() -> None:
    """Structural test: _workspace_spec_for_dispatch must reject container:*."""
    from modulo.core.bundled_runner.runner_dispatch import _workspace_spec_for_dispatch

    profile = _profile("runner_docker", config_json={"workspace_network": "container:abc123"})
    with pytest.raises(WorkspaceNetworkValidationError, match="container:"):
        _workspace_spec_for_dispatch(
            profile,
            org_id=uuid.uuid4(),
            run_id="run-123",
            node_id="node-9",
            run_uuid=uuid.uuid4(),
        )


def test_dispatch_rejects_bridge_network() -> None:
    """Structural test: _workspace_spec_for_dispatch must reject bridge."""
    from modulo.core.bundled_runner.runner_dispatch import _workspace_spec_for_dispatch

    profile = _profile("runner_docker", config_json={"workspace_network": "bridge"})
    with pytest.raises(WorkspaceNetworkValidationError, match="bridge"):
        _workspace_spec_for_dispatch(
            profile,
            org_id=uuid.uuid4(),
            run_id="run-123",
            node_id="node-9",
            run_uuid=uuid.uuid4(),
        )


def test_dispatch_rejects_default_network() -> None:
    """Structural test: _workspace_spec_for_dispatch must reject default."""
    from modulo.core.bundled_runner.runner_dispatch import _workspace_spec_for_dispatch

    profile = _profile("runner_docker", config_json={"workspace_network": "default"})
    with pytest.raises(WorkspaceNetworkValidationError, match="default"):
        _workspace_spec_for_dispatch(
            profile,
            org_id=uuid.uuid4(),
            run_id="run-123",
            node_id="node-9",
            run_uuid=uuid.uuid4(),
        )


def test_dispatch_accepts_valid_network() -> None:
    """Valid network name passes through dispatch."""
    from modulo.core.bundled_runner.runner_dispatch import _workspace_spec_for_dispatch

    profile = _profile("runner_docker", config_json={"workspace_network": "modulo-runner-workspace"})
    spec = _workspace_spec_for_dispatch(
        profile,
        org_id=uuid.uuid4(),
        run_id="run-123",
        node_id="node-9",
        run_uuid=uuid.uuid4(),
    )
    assert spec.workspace_network == "modulo-runner-workspace"


# ---------------------------------------------------------------------------
# Docker provider _resolve_network_mode — final safety net
# ---------------------------------------------------------------------------


def test_docker_provider_rejects_host_network_mode() -> None:
    """Structural test: _resolve_network_mode must reject host at the provider boundary."""
    from modulo.core.runtime_provider import WorkspaceSpec
    from modulo.core.runtime_provider.docker import DockerRuntimeProvider

    provider = DockerRuntimeProvider()
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        workspace_network="host",
    )
    with pytest.raises(WorkspaceNetworkValidationError, match="host"):
        provider._resolve_network_mode(spec)


def test_docker_provider_rejects_container_sharing() -> None:
    """Structural test: _resolve_network_mode must reject container:* at the provider boundary."""
    from modulo.core.runtime_provider import WorkspaceSpec
    from modulo.core.runtime_provider.docker import DockerRuntimeProvider

    provider = DockerRuntimeProvider()
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        workspace_network="container:abc123",
    )
    with pytest.raises(WorkspaceNetworkValidationError, match="container:"):
        provider._resolve_network_mode(spec)


def test_docker_provider_rejects_bridge() -> None:
    """Structural test: _resolve_network_mode must reject bridge."""
    from modulo.core.runtime_provider import WorkspaceSpec
    from modulo.core.runtime_provider.docker import DockerRuntimeProvider

    provider = DockerRuntimeProvider()
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        workspace_network="bridge",
    )
    with pytest.raises(WorkspaceNetworkValidationError, match="bridge"):
        provider._resolve_network_mode(spec)


def test_docker_provider_accepts_valid_network() -> None:
    """Valid network name passes through the Docker provider."""
    from modulo.core.runtime_provider import WorkspaceSpec
    from modulo.core.runtime_provider.docker import DockerRuntimeProvider

    provider = DockerRuntimeProvider()
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        workspace_network="modulo-runner-workspace",
    )
    assert provider._resolve_network_mode(spec) == "modulo-runner-workspace"


def test_docker_provider_defaults_to_configured_network() -> None:
    """None workspace_network falls back to the provider default."""
    from modulo.core.runtime_provider import WorkspaceSpec
    from modulo.core.runtime_provider.docker import DockerRuntimeProvider

    provider = DockerRuntimeProvider(workspace_network="my-custom-network")
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        workspace_network=None,
    )
    assert provider._resolve_network_mode(spec) == "my-custom-network"


# ---------------------------------------------------------------------------
# CRUD validation
# ---------------------------------------------------------------------------


def test_crud_validate_profile_workspace_network_rejects_host() -> None:
    """CRUD boundary must reject host in config_json.workspace_network."""
    from modulo.db.crud.environment_profile import validate_profile_workspace_network

    with pytest.raises(WorkspaceNetworkValidationError, match="host"):
        validate_profile_workspace_network({"workspace_network": "host"})


def test_crud_validate_profile_workspace_network_rejects_container() -> None:
    """CRUD boundary must reject container:* in config_json.workspace_network."""
    from modulo.db.crud.environment_profile import validate_profile_workspace_network

    with pytest.raises(WorkspaceNetworkValidationError, match="container:"):
        validate_profile_workspace_network({"workspace_network": "container:xyz"})


def test_crud_validate_profile_workspace_network_accepts_valid() -> None:
    """Valid network name passes CRUD validation."""
    from modulo.db.crud.environment_profile import validate_profile_workspace_network

    assert validate_profile_workspace_network({"workspace_network": "modulo-runner-workspace"}) is None


def test_crud_validate_profile_workspace_network_none_config() -> None:
    """None config_json is fine."""
    from modulo.db.crud.environment_profile import validate_profile_workspace_network

    assert validate_profile_workspace_network(None) is None


def test_crud_validate_profile_workspace_network_no_key() -> None:
    """config_json without workspace_network key is fine."""
    from modulo.db.crud.environment_profile import validate_profile_workspace_network

    assert validate_profile_workspace_network({"timeout_seconds": 3600}) is None
