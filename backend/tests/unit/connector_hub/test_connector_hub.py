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
)
from modulo.core.secrets_backend import create_secrets_backend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


@pytest.fixture(autouse=True)
def _reset_skip_warn_registry():
    """Keep the module-level skip-warning dedup registry isolated between tests (FAR-465)."""
    from modulo.core.connector_hub import _SKIP_WARN_SEEN

    _SKIP_WARN_SEEN.clear()
    yield
    _SKIP_WARN_SEEN.clear()


def _encrypt_raw(payload: str) -> bytes:
    """Encrypt a raw (non-JSON) string — how legacy bare-token credentials rows look."""
    return Fernet(_KEY.encode()).encrypt(payload.encode())


def _creds_capture_patch(captured: list[tuple[str, dict[str, Any]]]) -> Any:
    """Patch _build_connector to record (type_id, creds) per call, still building for real."""
    from modulo.core.connector_hub import _build_connector as real_build

    def _wrapper(type_id: str, config: dict[str, Any] | None, creds: dict[str, Any], **kwargs: Any) -> Any:
        captured.append((type_id, dict(creds)))
        return real_build(type_id, config, creds, **kwargs)

    return patch("modulo.core.connector_hub._build_connector", _wrapper)


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
    ("connector_type_id", "config_json", "credentials_json", "expected_type"),
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
    ids=["filesystem", "github", "monday", "trello", "asana", "notion", "confluence", "shortcut", "youtrack"],
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


async def test_initialise_youtrack_missing_base_url_is_skipped():
    """YouTrack without base_url is skipped by initialise — no placeholder default."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="youtrack",
        config_json={},
        credentials_ciphertext=_encrypt({"token": "yt_perm_token_123"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=json.dumps({"token": "yt_perm_token_123"})):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    with pytest.raises(ConnectorNotFoundError):
        hub.get(ci.id)


def test_get_unknown_raises():
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


async def test_multiple_hubs_coexist(tmp_path):
    """Separate ConnectorHub instances do not share connector registries.

    Each run gets its own hub; clearing one hub must not affect another.
    """
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    ci_a = _FakeCI(
        id=id1,
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path / "a")},
    )
    ci_b = _FakeCI(
        id=id2,
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path / "b")},
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    backend_a = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    backend_b = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    hub_a = ConnectorHub(secrets_backend=backend_a, org_id="org-a")
    hub_b = ConnectorHub(secrets_backend=backend_b, org_id="org-b")
    with (
        patch.object(backend_a, "get_secret", return_value="{}"),
        patch.object(backend_b, "get_secret", return_value="{}"),
    ):
        await hub_a.initialise([ci_a])
        await hub_b.initialise([ci_b])

    # Each hub exposes exactly its own connector.
    assert hub_a.connector_ids == frozenset({id1})
    assert hub_b.connector_ids == frozenset({id2})
    assert hub_a.get(id1) is not None
    assert hub_b.get(id2) is not None
    with pytest.raises(ConnectorNotFoundError):
        hub_a.get(id2)
    with pytest.raises(ConnectorNotFoundError):
        hub_b.get(id1)

    # Clearing one hub leaves the other fully intact.
    await hub_a.__aexit__(None, None, None)
    with pytest.raises(ConnectorNotFoundError):
        hub_a.get(id1)
    assert hub_b.connector_ids == frozenset({id2})
    assert hub_b.get(id2) is not None


async def test_multiple_hubs_concurrent_initialise(tmp_path):
    """Concurrent initialise of separate hubs does not interleave registries.

    Exercises the per-instance asyncio.Lock: simultaneous initialise calls on
    different hubs must each build exactly their own connector set.
    """
    hubs_and_cis = []
    for i in range(3):
        cid = uuid.uuid4()
        (tmp_path / f"dir{i}").mkdir(exist_ok=True)
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
        hub = ConnectorHub(secrets_backend=backend, org_id=f"org-{i}")
        ci = _FakeCI(
            id=cid,
            connector_type_id="filesystem",
            config_json={"base_path": str(tmp_path / f"dir{i}")},
        )
        hubs_and_cis.append((hub, backend, ci))

    async def _init(hub: ConnectorHub, backend: Any, ci: _FakeCI) -> None:
        with patch.object(backend, "get_secret", return_value="{}"):
            await hub.initialise([ci])

    await asyncio.gather(*(_init(hub, backend, ci) for hub, backend, ci in hubs_and_cis))

    for i, (hub, backend, ci) in enumerate(hubs_and_cis):
        assert hub.connector_ids == frozenset({ci.id}), f"hub {i} saw the wrong connector set"
        assert hub.get(ci.id) is not None
        for other_hub, _, _ in hubs_and_cis:
            if other_hub is not hub:
                with pytest.raises(ConnectorNotFoundError):
                    other_hub.get(ci.id)


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
        connector_type_id="monday",
        credentials_ciphertext=_encrypt({"api_key": "monday_fallback"}),
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
    assert connector.connector_type == "monday"


async def test_initialise_ciphertext_fallback_skip_on_bad_key():
    """credentials_ciphertext fallback that fails to decrypt is skipped."""
    other_key = Fernet.generate_key().decode()
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="monday",
        credentials_ciphertext=_encrypt({"api_key": "monday_fallback"}),
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
    assert not hub.connector_ids


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


async def test_initialise_programming_bug_logs_error():
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
    # FAR-495: unexpected failures land in hub.skipped too — every skip class
    # must reach the degraded-marker persist, not only typed errors.
    assert hub.skipped == {ci.id: "RuntimeError: boom"}


def test_record_skip_sanitizes_nul_and_truncates():
    """FAR-495: skip summaries are NUL-stripped and truncated to 2000 chars.

    Postgres rejects NUL bytes in SQL text — an unsanitized summary would fail
    the whole batch UPDATE so NO instance gets marked. 2000 matches the sibling
    ``last_health_check_error`` String(2000) column.
    """
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend)
    ci = _FakeCI(id=uuid.uuid4(), connector_type_id="github")
    exc = RuntimeError(f"bad\x00summary{'x' * 3000}")
    hub._record_skip(ci, exc)
    summary = hub.skipped[ci.id]
    assert "\x00" not in summary
    assert len(summary) == 2000
    assert summary.startswith("RuntimeError: badsummary")


async def test_initialise_records_healthy_instances(tmp_path):
    """FAR-495: successfully initialised instances are recorded in hub.healthy."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.healthy == {ci.id}
    assert not hub.skipped


async def test_initialise_records_skipped_instances(tmp_path):
    """FAR-495: instances that fail to initialise are recorded in hub.skipped with an error summary."""
    bad = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",  # requires a 'token' credential key
        credentials_ciphertext=_encrypt({}),  # creds lack the token key
    )
    healthy = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([bad, healthy])
    assert set(hub.skipped) == {bad.id}
    assert hub.skipped[bad.id].startswith("ValueError: Missing credential key 'token'")
    assert healthy.id not in hub.skipped
    assert hub.get(healthy.id) is not None
    # Symmetric tracking (FAR-495): the successful instance is in hub.healthy.
    assert hub.healthy == {healthy.id}


async def test_close_clears_skipped_and_healthy(tmp_path):
    """FAR-495: close() clears hub.skipped and hub.healthy along with the other hub state."""
    bad = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt({}),  # creds lack the token key -> skipped
    )
    healthy = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value="{}"):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([bad, healthy])
    assert set(hub.skipped) == {bad.id}
    assert hub.healthy == {healthy.id}
    hub.close()
    assert not hub.skipped
    assert not hub.healthy


async def test_initialise_resets_stale_skipped_and_healthy_from_aborted_pass(tmp_path):
    """FAR-498: initialise() resets stale skipped/healthy entries at entry.

    The attributes promise "during the last initialise() call". A hub whose
    previous pass aborted mid-loop (never reached close()) must not carry its
    stale entries into a new pass: simulate the aborted state by populating
    skipped/healthy manually, then run a fresh initialise() loop and assert
    only the new pass's results remain.
    """
    healthy = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="filesystem",
        config_json={"base_path": str(tmp_path)},
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend)
    # Simulate a previous aborted pass: entries for instances that are NOT
    # part of the new pass, populated exactly as the hub would have done.
    stale_skipped = uuid.uuid4()
    stale_healthy = uuid.uuid4()
    hub.skipped[stale_skipped] = "ValueError: stale from aborted pass"
    hub.healthy.add(stale_healthy)
    with patch.object(backend, "get_secret", return_value="{}"):
        await hub.initialise([healthy])
    assert stale_skipped not in hub.skipped
    assert not hub.skipped
    assert hub.healthy == {healthy.id}
    assert stale_healthy not in hub.healthy


# ---------------------------------------------------------------------------
# Skip-warning dedup (FAR-465)
# ---------------------------------------------------------------------------


async def test_skip_warning_dedup_full_traceback_once_per_process(caplog):
    """The first initialise of a misconfigured connector logs the full traceback;
    a second hub in the same process logs a concise repeat instead (FAR-465)."""
    import logging

    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt({}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    with (
        caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub"),
        patch.object(backend, "get_secret", return_value="{}"),
    ):
        hub1 = ConnectorHub(secrets_backend=backend)
        await hub1.initialise([ci])
        hub2 = ConnectorHub(secrets_backend=backend)
        await hub2.initialise([ci])

    skips = [rec for rec in caplog.records if "Skipping connector" in rec.message]
    assert len(skips) == 2
    assert skips[0].exc_info is not None
    assert "Missing credential key" in str(skips[0].exc_info[1])
    assert skips[1].exc_info is None
    assert "(repeat; full traceback logged earlier)" in skips[1].message
    assert not hub1.connector_ids
    assert not hub2.connector_ids


async def test_skip_warning_dedup_different_instance_id_logs_traceback(caplog):
    """A different instance id with the same problem logs its own full traceback
    on first sighting — dedup is keyed per instance, not per connector type."""
    import logging

    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")

    with (
        caplog.at_level(logging.WARNING, logger="modulo.core.connector_hub"),
        patch.object(backend, "get_secret", return_value="{}"),
    ):
        ci_a = _FakeCI(id=uuid.uuid4(), connector_type_id="github", credentials_ciphertext=_encrypt({}))
        hub_a = ConnectorHub(secrets_backend=backend)
        await hub_a.initialise([ci_a])

        ci_b = _FakeCI(id=uuid.uuid4(), connector_type_id="github", credentials_ciphertext=_encrypt({}))
        hub_b = ConnectorHub(secrets_backend=backend)
        await hub_b.initialise([ci_b])

    skips = [rec for rec in caplog.records if "Skipping connector" in rec.message]
    assert len(skips) == 2
    for rec in skips:
        assert rec.exc_info is not None
        assert "Missing credential key" in str(rec.exc_info[1])
        assert "(repeat; full traceback logged earlier)" not in rec.getMessage()


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


def test_acl_unknown_connector_raises():
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


# ---------------------------------------------------------------------------
# FAR-496: bare-token credentials_ciphertext read-side heal
# ---------------------------------------------------------------------------


async def test_initialise_bare_token_ciphertext_wraps_under_type_key_github():
    """FAR-496: a github instance whose ciphertext decrypts to a bare token
    string instantiates successfully — the bare scalar is wrapped under the
    type's own 'token' key (previously 'api_key', so instantiation always
    failed with "Missing credential key 'token'")."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt_raw("ghp_bare_token_123"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    captured: list[tuple[str, dict[str, Any]]] = []
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
        _creds_capture_patch(captured),
    ):
        settings = get_settings.return_value
        settings.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.GITHUB
    assert captured == [("github", {"token": "ghp_bare_token_123"})]


async def test_initialise_bare_token_ciphertext_rest_keeps_api_key_wrap():
    """FAR-496: rest is a multi-key (JSON-dict credentials) type — a bare
    scalar still wraps under the legacy 'api_key' key (unchanged)."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="rest",
        config_json={"base_url": "https://api.example.com"},
        credentials_ciphertext=_encrypt_raw("rest_bare_api_key"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    captured: list[tuple[str, dict[str, Any]]] = []
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
        _creds_capture_patch(captured),
    ):
        settings = get_settings.return_value
        settings.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert captured == [("rest", {"api_key": "rest_bare_api_key"})]


async def test_initialise_bare_token_ciphertext_jira_keeps_api_key_wrap():
    """FAR-496: jira requires multi-key JSON-dict credentials — a bare scalar
    still wraps under the legacy 'api_key' key (unchanged)."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="jira",
        config_json={"instance": "test.atlassian.net"},
        credentials_ciphertext=_encrypt_raw("jira_bare_token"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    captured: list[tuple[str, dict[str, Any]]] = []
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
        _creds_capture_patch(captured),
    ):
        settings = get_settings.return_value
        settings.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert captured == [("jira", {"api_key": "jira_bare_token"})]


async def test_initialise_json_dict_ciphertext_used_as_is_for_token_keyed_type():
    """FAR-496: a JSON-dict ciphertext is still used as-is — no re-wrapping
    happens for token-keyed types when the plaintext is already a dict."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt({"token": "ghp_dict_token"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    captured: list[tuple[str, dict[str, Any]]] = []
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
        _creds_capture_patch(captured),
    ):
        settings = get_settings.return_value
        settings.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == ConnectorType.GITHUB
    assert captured == [("github", {"token": "ghp_dict_token"})]


@pytest.mark.parametrize(
    ("connector_type_id", "bare_token", "expected_key"),
    [
        ("slack", "xoxb_bare_token", "bot_token"),
        ("asana", "asana_pat_bare", "personal_access_token"),
        ("monday", "monday_key_bare", "api_key"),
    ],
)
async def test_initialise_bare_token_ciphertext_wraps_under_type_specific_key(
    connector_type_id, bare_token, expected_key
):
    """FAR-496: types with a non-'token' credential key (slack bot_token,
    asana personal_access_token) and single-'api_key' types (monday) wrap
    bare scalars under their own key."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id=connector_type_id,
        credentials_ciphertext=_encrypt_raw(bare_token),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    captured: list[tuple[str, dict[str, Any]]] = []
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
        _creds_capture_patch(captured),
    ):
        settings = get_settings.return_value
        settings.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    connector = hub.get(ci.id)
    assert connector.connector_type == connector_type_id
    assert captured == [(connector_type_id, {expected_key: bare_token})]
