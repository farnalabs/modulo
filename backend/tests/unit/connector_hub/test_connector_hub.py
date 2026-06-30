"""Unit tests for ConnectorHub lifecycle."""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.core.connector_hub import (
    ConnectorDecryptError,
    ConnectorHub,
    ConnectorNotFoundError,
)
from modulo.core.secrets_backend import create_secrets_backend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=lambda: _encrypt({}))
    visibility: str = "org"
    allowed_operations: list[str] | None = None


# ---------------------------------------------------------------------------
# ConnectorHub lifecycle
# ---------------------------------------------------------------------------


async def test_initialise_creates_filesystem_connector(tmp_path):
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.FILESYSTEM


async def test_initialise_creates_github_connector():
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt({"token": "ghp_test"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"token": "ghp_test"}'):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.GITHUB


async def test_initialise_creates_trello_connector():
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="trello",
        credentials_ciphertext=_encrypt({"api_key": "trello_key", "token": "trello_token"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value='{"api_key": "trello_key", "token": "trello_token"}'):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.TRELLO


async def test_get_unknown_raises():
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend)
    unknown_id = uuid.uuid4()
    with pytest.raises(ConnectorNotFoundError) as exc_info:
        hub.get(unknown_id)
    assert exc_info.value.connector_id == unknown_id


async def test_aexit_clears_connectors(tmp_path):
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        async with hub:
            await hub.initialise([ci])
            assert hub.get(ci.id) is not None

    # After __aexit__, hub is cleared
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_connector_ids_property(tmp_path):
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    base = {"base_path": str(tmp_path)}
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise(
            [
                _FakeCI(id=id1, connector_type_id="filesystem", config_json=base),
                _FakeCI(id=id2, connector_type_id="filesystem", config_json=base),
            ]
        )
    assert hub.connector_ids == frozenset({id1, id2})


async def test_wrong_fernet_key_raises_decrypt_error():
    other_key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
    )
    backend = create_secrets_backend(fernet_key=other_key, backend_name="fernet")
    with patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))):
        hub = ConnectorHub(secrets_backend=backend)
        with pytest.raises(ConnectorDecryptError) as exc_info:
            await hub.initialise([ci])
    assert exc_info.value.connector_id == ci.id


async def test_missing_base_path_in_config_raises():
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={},  # no base_path
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        with pytest.raises(ValueError, match="base_path"):
            await hub.initialise([ci])


async def test_unknown_connector_type_raises():
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="nonexistent",
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        with pytest.raises(ValueError, match="Unknown connector type"):
            await hub.initialise([ci])


async def test_initialise_plugin_fallback_connector():
    """When a connector type is not built-in, hub falls back to the plugin registry."""
    from modulo.connectors.base import ConnectorBase
    from modulo.core.plugin_registry import PluginManifest, PluginRegistry

    class _PluginConnector(ConnectorBase):
        @property
        def connector_type(self) -> ConnectorType:
            return ConnectorType.CUSTOM

        async def health_check(self) -> "HealthResult":
            from modulo.connectors.base import HealthResult

            return HealthResult(ok=True)

        async def query(self, q: ConnectorQuery) -> ConnectorResult:
            return ConnectorResult(records=[{"p": True}])

        async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
            return {"p": True}

    def _plugin_builder(config: dict, creds: dict) -> ConnectorBase:
        return _PluginConnector()

    reg = PluginRegistry()
    reg.register_connector_type(
        "my_custom_connector",
        _plugin_builder,
        PluginManifest(PLUGIN_ID="pkg-demo", display_name="Demo", description="", version="1"),
    )

    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="my_custom_connector",
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", return_value="{}"),
        patch("modulo.core.connector_hub.get_plugin_registry", return_value=reg),
    ):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])

    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.CUSTOM


async def test_initialise_plugin_fallback_not_registered_raises():
    """When a connector type is not built-in and not in the plugin registry, raise ValueError."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="some_unknown_type",
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", return_value="{}"),
    ):
        hub = ConnectorHub(secrets_backend=backend)
        with pytest.raises(ValueError, match="Unknown connector type"):
            await hub.initialise([ci])


async def test_initialise_is_additive(tmp_path):
    """Multiple initialise calls accumulate connectors (they don't clear previous ones)."""
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    base = {"base_path": str(tmp_path)}
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([_FakeCI(id=id1, connector_type_id="filesystem", config_json=base)])
        await hub.initialise([_FakeCI(id=id2, connector_type_id="filesystem", config_json=base)])
    # Both connectors are accessible after two initialise calls.
    hub.get(id1)
    hub.get(id2)


# ---------------------------------------------------------------------------
# Shell connector hub integration
# ---------------------------------------------------------------------------


class _HubFakeRuntimeProvider:
    """Minimal RuntimeProvider test double for hub integration tests."""

    async def create_workspace(self, spec: Any) -> str:
        return "ws-fake"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        from modulo.core.runtime_provider import ExecResult

        return ExecResult(exit_code=0, stdout="", stderr="")

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


async def test_initialise_creates_shell_connector():
    """Shell connector can be created via the hub when a RuntimeProvider is provided."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="shell",
        config_json={"allowed_commands": ["echo", "ls"]},
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    _HubFakeRuntimeProvider()
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.SHELL


async def test_initialise_shell_no_runtime_provider_raises():
    """Shell connector initialisation without a RuntimeProvider should raise ValueError."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="shell",
        config_json={"allowed_commands": ["echo"]},
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        with pytest.raises(ValueError, match="RuntimeProvider"):
            await hub.initialise([ci])
