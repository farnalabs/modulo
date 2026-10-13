"""Unit tests for modulo.core.trigger_secrets (FAR-1735 bounded resolver).

The shared resolution policy for the five delivery-time trigger-secret
readers: non-token values are legacy plaintext accepted with a LOUD dated
warning; token-shaped values decrypt or FAIL CLOSED.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from modulo.auth.secret_storage import DecryptionError
from modulo.core.trigger_secrets import (
    LEGACY_PLAINTEXT_SHIM_SINCE,
    resolve_trigger_secret_scoped,
)

_FERNET_KEY = Fernet.generate_key().decode()
_ORG_ID = uuid.uuid4()
_TRIGGER_ID = uuid.uuid4()


def _session_mock() -> AsyncMock:
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=True)
    session.info = {}
    return session


async def test_legacy_plaintext_accepted_with_dated_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A NON-token stored value is legacy pre-0306 plaintext: returned
    unchanged so pre-migration deployments keep delivering, and the
    ``trigger_secret.legacy_plaintext`` warning names the trigger, secret,
    context and the shim's date so a future plaintext write is surfaced."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="modulo.core.trigger_secrets"):
        result = await resolve_trigger_secret_scoped(
            _session_mock(),
            stored_raw="legacy-plaintext-secret",
            fernet_key=_FERNET_KEY,
            org_id=_ORG_ID,
            trigger_id=_TRIGGER_ID,
            config_key="hmac_secret",
            context="webhook_engine",
        )

    assert result == "legacy-plaintext-secret"
    assert "trigger_secret.legacy_plaintext" in caplog.text
    assert str(_TRIGGER_ID) in caplog.text
    assert "hmac_secret" in caplog.text
    assert "webhook_engine" in caplog.text
    assert LEGACY_PLAINTEXT_SHIM_SINCE in caplog.text


async def test_token_shape_decrypts_via_scope_bound_helper() -> None:
    """A token-shaped value decrypts through the REAL scope-bound helper
    (no decode mock) and returns the plaintext."""
    token = Fernet(_FERNET_KEY.encode()).encrypt(b"decrypted-secret").decode()

    result = await resolve_trigger_secret_scoped(
        _session_mock(),
        stored_raw=token,
        fernet_key=_FERNET_KEY,
        org_id=_ORG_ID,
        trigger_id=_TRIGGER_ID,
        config_key="signing_secret",
        context="slack_route",
    )

    assert result == "decrypted-secret"


async def test_undecryptable_token_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    """A token-shaped value that cannot be decrypted raises
    :class:`DecryptionError` — ciphertext is never returned as the secret,
    and never falls back to the raw stored value."""
    import logging as _logging

    other_key = Fernet.generate_key().decode()
    alien_token = Fernet(other_key.encode()).encrypt(b"alien").decode()

    with (
        caplog.at_level(_logging.ERROR, logger="modulo.core.trigger_secrets"),
        pytest.raises(DecryptionError),
    ):
        await resolve_trigger_secret_scoped(
            _session_mock(),
            stored_raw=alien_token,
            fernet_key=_FERNET_KEY,
            org_id=_ORG_ID,
            trigger_id=_TRIGGER_ID,
            config_key="hmac_secret",
            context="webhooks_route",
        )
