"""Unit tests for ConnectorHub lifecycle."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Self
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorPermissionError,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.core.connector_hub import (
    ConnectorHub,
    ConnectorNotFoundError,
    _build_connector,
)
from modulo.core.secrets_backend import create_secrets_backend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

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


@pytest.mark.parametrize(
    "connector_type_id,config_json,credentials_json,expected_type",
    [
        ("filesystem", {"base_path": "/tmp"}, {}, ConnectorType.FILESYSTEM),
        ("github", {}, {"token": "ghp_test"}, ConnectorType.GITHUB),
        ("monday", {}, {"api_key": "monday_key"}, ConnectorType.MONDAY),
        ("trello", {}, {"api_key": "trello_key", "token": "trello_token"}, ConnectorType.TRELLO),
        ("asana", {}, {"personal_access_token": "asana_pat_123"}, ConnectorType.ASANA),
        ("notion", {}, {"token": "ntn_test_token"}, ConnectorType.NOTION),
        (
            "confluence",
            {"instance": "my-domain.atlassian.net/wiki"},
            {"token": "confluence_token"},
            ConnectorType.CONFLUENCE,
        ),
        ("shortcut", {}, {"token": "shortcut_token"}, ConnectorType.SHORTCUT),
        (
            "youtrack",
            {"base_url": "https://youtrack.example.com/api"},
            {"token": "yt_perm_token_123"},
            ConnectorType.YOUTRACK,
        ),
    ],
)
async def test_initialise_creates_connector(connector_type_id, config_json, credentials_json, expected_type, tmp_path):
    if connector_type_id == "filesystem":
        config_json["base_path"] = str(tmp_path)
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id=connector_type_id,
        config_json=config_json,
        credentials_ciphertext=_encrypt(credentials_json),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=json.dumps(credentials_json)):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == expected_type


def test_build_youtrack_requires_base_url():
    """_build_connector requires base_url for youtrack — no placeholder default."""
    with pytest.raises(ValueError, match="requires 'base_url'"):
        _build_connector("youtrack", {}, {"token": "yt_perm_token_123"})
    connector = _build_connector(
        "youtrack",
        {"base_url": "https://youtrack.example.com/api"},
        {"token": "yt_perm_token_123"},
    )
    assert connector.connector_type == ConnectorType.YOUTRACK


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


async def test_wrong_fernet_key_skips_connector():
    """Decrypt errors are logged and the connector is skipped (not propagated)."""
    other_key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
    )
    backend = create_secrets_backend(fernet_key=other_key, backend_name="fernet")
    with patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_missing_base_path_in_config_skips():
    """Missing required config logs warning and skips the connector."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={},  # no base_path
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_unknown_connector_type_skips():
    """Unknown connector types are skipped with a warning."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="nonexistent",
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


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


async def test_initialise_plugin_fallback_not_registered_skips():
    """When a connector type is not built-in and not in the plugin registry, skip with warning."""
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
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_initialise_is_idempotent(tmp_path):
    """Multiple initialise calls are idempotent — second call is skipped due to _initialised guard."""
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    base = {"base_path": str(tmp_path)}
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([_FakeCI(id=id1, connector_type_id="filesystem", config_json=base)])
        # Second call is a no-op (ConnectorHub already initialised)
        await hub.initialise([_FakeCI(id=id2, connector_type_id="filesystem", config_json=base)])
    # Only the first connector is accessible
    assert hub.get(id1) is not None
    with pytest.raises(ConnectorNotFoundError):
        hub.get(id2)


# ---------------------------------------------------------------------------
# initialise() secret handling edge cases
# ---------------------------------------------------------------------------


async def test_initialise_secret_timeout_skips_connector():
    """get_secret timing out is logged and the connector is skipped."""
    ci = _FakeCI(id=uuid.uuid4(), connector_type_id="filesystem", config_json={"base_path": "/tmp"})
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", side_effect=TimeoutError):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_initialise_decrypt_error_skips_connector():
    """JSON that is not a dict triggers ConnectorDecryptError and the connector is skipped."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": "/tmp"},
        credentials_ciphertext=_encrypt([]),  # list, not dict
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="[]"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_initialise_non_json_secret_skips_connector():
    """Malformed JSON in the secret triggers ConnectorDecryptError and skips the connector."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": "/tmp"},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="not-json{{{"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_initialise_ciphertext_fallback_uses_column():
    """When the secrets backend raises KeyError, credentials_ciphertext column is used."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="linear",
        credentials_ciphertext=_encrypt({"api_key": "lin_fallback"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        settings = get_settings.return_value
        settings.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == "linear"


async def test_initialise_ciphertext_fallback_skip_on_bad_key():
    """credentials_ciphertext fallback that fails to decrypt is skipped."""
    other_key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="linear",
        credentials_ciphertext=_encrypt({"api_key": "lin_fallback"}),
    )
    backend = create_secrets_backend(fernet_key=other_key, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        settings = get_settings.return_value
        settings.fernet_key = other_key
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


async def test_initialise_ciphertext_fallback_empty_column_uses_empty():
    """Empty credentials_ciphertext with no backend secret defaults to empty creds."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": "/tmp"},
        credentials_ciphertext=b"",
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id) is not None


async def test_initialise_none_secret_uses_empty_creds(tmp_path):
    """get_secret returning None falls back to empty credentials and still builds the connector."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=None):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id) is not None


async def test_initialise_double_guard_inside_lock_warns(tmp_path, caplog):
    """The second _initialised check inside the lock logs a warning when another coroutine wins."""
    import logging

    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    class _RacingLock:
        """A lock whose __aenter__ flips _initialised, simulating a concurrent initialise."""

        def __init__(self, hub: ConnectorHub) -> None:
            self._hub = hub

        async def __aenter__(self) -> Self:
            self._hub._initialised = True
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    hub = ConnectorHub(secrets_backend=backend)
    hub._init_lock = _RacingLock(hub)

    with (
        patch.object(backend, "get_secret", return_value="{}"),
        caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub"),
    ):
        await hub.initialise([ci])

    assert any("already initialised" in rec.message for rec in caplog.records)
    # Nothing was built because the inner guard fired before the loop.
    assert hub.connector_ids == frozenset()


async def test_initialise_cancelled_error_propagates():
    """CancelledError during initialise is re-raised, not swallowed."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": "/tmp"},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", side_effect=asyncio.CancelledError):
        hub = ConnectorHub(secrets_backend=backend)
        with pytest.raises(asyncio.CancelledError):
            await hub.initialise([ci])


async def test_initialise_programming_bug_logs_error(caplog):
    """Unexpected exceptions during initialise are logged as programming bugs and skipped."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": "/tmp"},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", return_value="{}"),
        patch(
            "modulo.core.connector_hub._build_connector",
            side_effect=RuntimeError("boom"),
        ),
    ):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


# ---------------------------------------------------------------------------
# ConnectorHub API surface
# ---------------------------------------------------------------------------


async def test_acl_returns_acl_for_connector(tmp_path):
    """acl() returns the stored ACL for a registered connector."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        visibility="team",
        allowed_operations=["read", "write"],
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    acl = hub.acl(ci.id)
    assert acl.visibility == "team"
    assert acl.allowed_operations == frozenset({"read", "write"})


async def test_acl_unknown_connector_raises():
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend)
    with pytest.raises(ConnectorNotFoundError):
        hub.acl(uuid.uuid4())


async def test_get_checks_operation_acl(tmp_path):
    """get() with an operation not in allowed_operations raises ConnectorPermissionError."""
    from modulo.connectors.base import ConnectorPermissionError

    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        allowed_operations=["read"],
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorPermissionError):
        hub.get(ci.id, operation="write")


async def test_sample_propagates_query_results(tmp_path):
    """sample() returns query records and enforces the read ACL."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        allowed_operations=["read"],
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    records = await hub.sample(ci.id, "directory", filters={"path": str(tmp_path)}, limit=5)
    assert isinstance(records, list)


async def test_sample_enforces_read_acl(tmp_path):
    """sample() raises ConnectorPermissionError when 'read' is not allowed."""
    from modulo.connectors.base import ConnectorPermissionError

    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        allowed_operations=["write"],
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorPermissionError):
        await hub.sample(ci.id, "directory")


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
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.SHELL


# ---------------------------------------------------------------------------
# ACL enforcement via _TracedConnector
# ---------------------------------------------------------------------------


async def test_acl_blocks_write(tmp_path):
    """ConnectorACL is enforced by _TracedConnector — writes are blocked when
    allowed_operations omits 'write'."""
    from modulo.connectors.base import ConnectorPayload

    ci_id = uuid.uuid4()
    ci = _FakeCI(
        id=ci_id,
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt({}),
        visibility="org",
        allowed_operations=["read"],
    )
    key = Fernet.generate_key().decode()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])

    connector = hub.get(ci_id)

    with pytest.raises(ConnectorPermissionError):
        await connector.write(
            ConnectorPayload(resource="file", data={"content": "secret", "path": str(tmp_path / "acl_test.txt")})
        )


async def test_acl_allows_read(tmp_path):
    """ACL with 'read' permitted — query and sample pass."""
    ci_id = uuid.uuid4()
    ci = _FakeCI(
        id=ci_id,
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
        credentials_ciphertext=_encrypt({}),
        visibility="org",
        allowed_operations=["read"],
    )
    key = Fernet.generate_key().decode()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])

    records = await hub.sample(ci_id, "directory", filters={"path": str(tmp_path)})
    assert isinstance(records, list)


async def test_initialise_shell_no_runtime_provider_creates_connector():
    """Shell connector initialised without RuntimeProvider succeeds at init
    but raises ValueError on query/write."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="shell",
        config_json={"allowed_commands": ["echo"]},
        credentials_ciphertext=_encrypt({}),
    )
    from modulo.connectors.base import ConnectorQuery

    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])

    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.SHELL
    with pytest.raises(ValueError, match="Runtime provider not configured"):
        await connector.query(ConnectorQuery(resource="directory"))
