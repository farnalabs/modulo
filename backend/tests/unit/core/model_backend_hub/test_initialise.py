"""Unit tests for ModelBackendHub.initialise() error handling and fallback parsing.

Requires no DB — uses duck-typed rows with an AsyncMock secrets backend, and the
hermetic ``custom`` provider for registered backends.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.model_backend_hub import (
    ModelBackendHub,
    _extract_fixture_map,
)
from modulo.model_backends.stub import StubModelBackend


@pytest.fixture
async def hub() -> AsyncGenerator[ModelBackendHub, None]:
    async with ModelBackendHub() as h:
        yield h


def _row(
    *,
    id_: uuid.UUID | None = None,
    provider: str = "custom",
    model_id: str = "stub",
    default_params: dict | None = None,
    credentials_ciphertext: bytes = b"",
    fallback_backend_ids: object = (),
) -> MagicMock:
    row = MagicMock()
    row.id = id_ or uuid.uuid4()
    row.provider = provider
    row.model_id = model_id
    row.credentials_ciphertext = credentials_ciphertext
    row.default_params = default_params or {}
    row.fallback_backend_ids = fallback_backend_ids
    return row


def _secrets(*, secret: str = "", error: Exception | None = None) -> AsyncMock:
    secrets_backend = AsyncMock()
    if error is not None:
        secrets_backend.get_secret = AsyncMock(side_effect=error)
    else:
        secrets_backend.get_secret = AsyncMock(return_value=secret)
    return secrets_backend


# ---------------------------------------------------------------------------
# initialise() — None guard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_none_instances_raises(hub: ModelBackendHub) -> None:
    with pytest.raises(ValueError, match="instances must not be None"):
        await hub.initialise(None, secrets_backend=_secrets(secret="{}"))


# ---------------------------------------------------------------------------
# initialise() — no instances provided
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_no_instances_registers_nothing(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    await hub.initialise([], secrets_backend=_secrets(secret="{}"))
    assert not hub.backend_ids
    assert "No backends were registered" in caplog.text


# ---------------------------------------------------------------------------
# initialise() — secret fetch failure paths
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_skips_on_secret_timeout(hub: ModelBackendHub, caplog: pytest.LogCaptureFixture) -> None:
    row = _row()
    await hub.initialise([row], secrets_backend=_secrets(error=TimeoutError("slow secret")))
    assert row.id not in hub.backend_ids
    assert "Timeout fetching secret" in caplog.text


@pytest.mark.anyio
async def test_initialise_skips_on_malformed_secret_json(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    row = _row()
    await hub.initialise([row], secrets_backend=_secrets(secret="not json at all"))
    assert row.id not in hub.backend_ids
    assert "Malformed secret JSON" in caplog.text


@pytest.mark.anyio
async def test_initialise_skips_on_non_object_secret(hub: ModelBackendHub, caplog: pytest.LogCaptureFixture) -> None:
    row = _row()
    await hub.initialise([row], secrets_backend=_secrets(secret='["a", "b"]'))
    assert row.id not in hub.backend_ids
    assert "not a JSON object" in caplog.text


@pytest.mark.anyio
async def test_initialise_skips_when_secret_missing_without_ciphertext(
    hub: ModelBackendHub,
) -> None:
    row = _row()
    del row.credentials_ciphertext
    await hub.initialise([row], secrets_backend=_secrets(error=KeyError("missing")))
    assert row.id not in hub.backend_ids


# ---------------------------------------------------------------------------
# initialise() — credentials_ciphertext decrypt path
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_uses_credentials_ciphertext_when_secret_backend_misses(
    hub: ModelBackendHub,
) -> None:
    """When get_secret raises KeyError, a valid credentials_ciphertext must be
    decrypted via the configured Fernet key and used as the backend secret."""
    fernet_key = Fernet.generate_key()
    row = _row(
        provider="custom",
        credentials_ciphertext=Fernet(fernet_key).encrypt(b"sk-fallback-123"),
        default_params={"fixture_map": {"hello": "world"}},
    )
    with patch(
        "modulo.settings.get_settings",
        return_value=SimpleNamespace(fernet_key=fernet_key.decode(), fernet_key_old=""),
    ):
        await hub.initialise([row], secrets_backend=_secrets(error=KeyError("fallthrough to ciphertext")))

    backend = hub._backends[row.id]
    assert isinstance(backend._stub, StubModelBackend)
    assert backend._stub.fixture_map == {"hello": "world"}
    assert hub._healthy[row.id] is True


@pytest.mark.anyio
async def test_initialise_skips_when_ciphertext_fails_to_decrypt(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    """An undecryptable credentials_ciphertext must surface as BackendDecryptError
    and the faulty row must be skipped, not crash initialise()."""
    row = _row(credentials_ciphertext=b"this is not a fernet token")
    with patch(
        "modulo.settings.get_settings",
        return_value=SimpleNamespace(fernet_key=Fernet.generate_key().decode(), fernet_key_old=""),
    ):
        await hub.initialise([row], secrets_backend=_secrets(error=KeyError("missing")))

    assert row.id not in hub.backend_ids
    assert "Failed to initialise backend" in caplog.text


# ---------------------------------------------------------------------------
# initialise() — fallback_backend_ids parsing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_non_iterable_fallback_ids_is_skipped(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    row = _row(fallback_backend_ids=123)
    await hub.initialise([row], secrets_backend=_secrets(secret="{}"))
    assert row.id in hub.backend_ids
    assert row.id not in hub._fallbacks
    assert "Non-iterable fallback_backend_ids" in caplog.text


@pytest.mark.anyio
async def test_initialise_invalid_fallback_id_string_is_skipped(
    hub: ModelBackendHub,
) -> None:
    row = _row(fallback_backend_ids=["not-a-uuid"])
    await hub.initialise([row], secrets_backend=_secrets(secret="{}"))
    assert row.id not in hub._fallbacks


@pytest.mark.anyio
async def test_initialise_accepts_uuid_object_fallback_ids(hub: ModelBackendHub) -> None:
    fallback_id = uuid.uuid4()
    row = _row(fallback_backend_ids=[fallback_id])
    await hub.initialise([row], secrets_backend=_secrets(secret="{}"))
    assert hub._fallbacks[row.id] == [fallback_id]


@pytest.mark.anyio
async def test_initialise_skips_unexpected_fallback_id_types(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    fallback_id = uuid.uuid4()
    row = _row(fallback_backend_ids=[fallback_id, 42])
    await hub.initialise([row], secrets_backend=_secrets(secret="{}"))
    assert hub._fallbacks[row.id] == [fallback_id]
    assert "Unexpected fallback ID type" in caplog.text


# ---------------------------------------------------------------------------
# _extract_fixture_map — fixture_map precedence and coercion
# ---------------------------------------------------------------------------


def test_extract_fixture_map_prefers_default_params() -> None:
    assert _extract_fixture_map(
        {"fixture_map": {"from_creds": "a"}},
        {"fixture_map": {"from_params": "b"}},
    ) == {"from_params": "b"}


def test_extract_fixture_map_falls_back_to_creds() -> None:
    assert _extract_fixture_map(
        {"fixture_map": {"ping": "pong"}},
        {},
    ) == {"ping": "pong"}


def test_extract_fixture_map_coerces_keys_and_values() -> None:
    assert _extract_fixture_map(
        {"fixture_map": {1: 2, "b": None}},
        {},
    ) == {"1": "2", "b": "None"}


def test_extract_fixture_map_returns_empty_when_absent() -> None:
    assert not _extract_fixture_map({"api_key": "x"}, {"default_params": 1})
