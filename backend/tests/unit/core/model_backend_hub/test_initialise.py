"""Unit tests for ModelBackendHub.initialise() error handling and fallback parsing.

Requires no DB — uses duck-typed rows with an AsyncMock secrets backend, and the
hermetic ``custom`` provider for registered backends.
"""

from __future__ import annotations

import logging
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
# initialise() — no stored credentials at all (FAR-1526)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_initialise_missing_credentials_is_typed_and_actionable(
    hub: ModelBackendHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1526: no secrets row + no ciphertext must NOT surface as a bare
    ``KeyError('<backend-uuid>')``.

    The prod worker logged that KeyError (with a full traceback) on every run's
    hub init for backend ``4a170ce2-4598-4c38-b927-f9b491275ad6``, which reads
    exactly like a cache/registry lookup bug. The failure must instead name the
    backend, the provider and the remedy.
    """
    row = _row(provider="openrouter", credentials_ciphertext=b"")
    await hub.initialise(
        [row],
        secrets_backend=_secrets(error=KeyError(str(row.id))),
    )

    assert row.id not in hub.backend_ids
    assert "has no stored credentials" in caplog.text
    assert "set its API key" in caplog.text
    assert f"KeyError: '{row.id}'" not in caplog.text


@pytest.mark.anyio
async def test_initialise_missing_credentials_without_provider_omits_provider_detail(
    hub: ModelBackendHub,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1526: a row with no provider still gets the typed, actionable error —
    the provider clause is simply omitted when there is nothing to name."""
    row = _row(provider="", credentials_ciphertext=b"")
    await hub.initialise(
        [row],
        secrets_backend=_secrets(error=KeyError(str(row.id))),
    )

    assert row.id not in hub.backend_ids
    assert "has no stored credentials" in caplog.text
    assert "set its API key" in caplog.text
    assert "(provider" not in caplog.text


@pytest.mark.anyio
async def test_initialise_logs_repeated_failure_without_traceback_flood(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """FAR-1526: the failure recurs on EVERY run's hub init — the full detail is
    logged once per process, then a compact repeat line that still names the
    backend (never silent, never a traceback per run)."""
    row = _row(provider="openrouter", credentials_ciphertext=b"")
    secrets = _secrets(error=KeyError(str(row.id)))

    async with ModelBackendHub() as first:
        await first.initialise([row], secrets_backend=secrets)
    first_text = caplog.text
    assert "Failed to initialise backend" in first_text
    assert "has no stored credentials" in first_text

    caplog.clear()

    async with ModelBackendHub() as second:
        await second.initialise([row], secrets_backend=secrets)

    assert "repeat; full detail logged earlier" in caplog.text
    assert str(row.id) in caplog.text
    # No second traceback flood for the same backend id.
    assert "Traceback (most recent call last)" not in caplog.text


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
# initialise() — per-backend isolation (S3776 refactor regression tests)
# ---------------------------------------------------------------------------


def _secrets_per_backend(
    secrets: dict[str, str],
    missing: frozenset[str] | set[str] = frozenset(),
) -> AsyncMock:
    """Secrets backend that resolves per-backend-id: *missing* ids raise KeyError."""

    async def _get_secret(key: str) -> str:
        if key in missing:
            raise KeyError(key)
        return secrets[key]

    secrets_backend = AsyncMock()
    secrets_backend.get_secret = AsyncMock(side_effect=_get_secret)
    return secrets_backend


@pytest.mark.anyio
async def test_initialise_bad_backends_skipped_while_good_ones_register(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    """One broken backend row must not prevent the good ones from registering.

    Covers three failure classes: an unknown provider (dispatch ValueError),
    a malformed secret JSON (skip path), and a valid custom-provider row.
    """
    good = _row(provider="custom")
    bad_build = _row(provider="no_such_provider_xyz")
    bad_secret = _row(provider="custom")
    secrets = _secrets_per_backend(
        {
            str(good.id): '{"api_key": "sk-good"}',
            str(bad_build.id): '{"api_key": "sk-x"}',
            str(bad_secret.id): "{not json at all",
        }
    )

    await hub.initialise([good, bad_build, bad_secret], secrets_backend=secrets)

    assert good.id in hub.backend_ids
    assert bad_build.id not in hub.backend_ids
    assert bad_secret.id not in hub.backend_ids
    assert "Failed to initialise backend" in caplog.text
    assert "Malformed secret JSON" in caplog.text


@pytest.mark.anyio
async def test_initialise_never_logs_credential_values(hub: ModelBackendHub, caplog: pytest.LogCaptureFixture) -> None:
    """No emitted log record may contain a credential VALUE.

    Exercises every logging path in the resolution pipeline with the same
    canary secret: successful registration, malformed secret JSON (whose
    document embeds the value), a non-object secret holding the value, an
    undecryptable ciphertext whose plaintext IS the value, and a
    missing-credentials row. Asserts at the record level (message + captured
    traceback) as well as on the aggregate caplog text.
    """
    canary = "sk-CANARY-VALUE-9f3e2a"
    caplog.set_level(logging.INFO, logger="modulo.core.model_backend_hub")

    good = _row(provider="custom")
    malformed = _row(provider="custom")
    non_object = _row(provider="custom")
    undecryptable = _row(provider="custom", credentials_ciphertext=b"not-a-fernet-token")
    missing = _row(provider="custom", credentials_ciphertext=b"")
    wrong_key_row = _row(
        provider="custom",
        credentials_ciphertext=Fernet(Fernet.generate_key()).encrypt(canary.encode()),
    )
    secrets = _secrets_per_backend(
        {
            str(good.id): f'{{"api_key": "{canary}"}}',
            str(malformed.id): f'{{"api_key": "{canary}"',
            str(non_object.id): f'["{canary}"]',
        },
        missing=frozenset({str(undecryptable.id), str(missing.id), str(wrong_key_row.id)}),
    )

    with patch(
        "modulo.settings.get_settings",
        return_value=SimpleNamespace(fernet_key=Fernet.generate_key().decode(), fernet_key_old=""),
    ):
        await hub.initialise(
            [good, malformed, non_object, undecryptable, missing, wrong_key_row],
            secrets_backend=secrets,
        )

    assert good.id in hub.backend_ids
    for broken in (malformed, non_object, undecryptable, missing, wrong_key_row):
        assert broken.id not in hub.backend_ids
    # Every logging path fired — the assertion below is therefore not vacuous.
    assert "Malformed secret JSON" in caplog.text
    assert "not a JSON object" in caplog.text
    assert "Failed to decrypt credentials_ciphertext" in caplog.text
    assert "has no stored credentials" in caplog.text
    for record in caplog.records:
        assert canary not in record.getMessage()
        if record.exc_text is not None:
            assert canary not in record.exc_text
    assert canary not in caplog.text


@pytest.mark.anyio
async def test_initialise_decrypt_failure_fails_closed_for_that_backend_only(
    hub: ModelBackendHub, caplog: pytest.LogCaptureFixture
) -> None:
    """A ciphertext that will not decrypt fails CLOSED for that backend only:
    it is skipped with a logged failure (no value), while a healthy sibling
    row still registers."""
    bad = _row(provider="custom", credentials_ciphertext=b"corrupt-ciphertext")
    good = _row(provider="custom")
    secrets = _secrets_per_backend(
        {str(good.id): "{}"},
        missing=frozenset({str(bad.id)}),
    )

    with patch(
        "modulo.settings.get_settings",
        return_value=SimpleNamespace(fernet_key=Fernet.generate_key().decode(), fernet_key_old=""),
    ):
        await hub.initialise([bad, good], secrets_backend=secrets)

    assert bad.id not in hub.backend_ids
    assert good.id in hub.backend_ids
    assert "Failed to decrypt credentials_ciphertext" in caplog.text
    assert "Failed to initialise backend" in caplog.text


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
