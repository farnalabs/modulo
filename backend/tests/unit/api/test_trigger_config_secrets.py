"""Unit tests for trigger config secret encryption and round-trip."""

from cryptography.fernet import Fernet

from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.api.routes.triggers import _encrypt_trigger_config_secrets
from modulo.auth.secret_storage import decode_stored_secret

_FERNET_KEY = Fernet.generate_key().decode()


def test_encrypt_trigger_config_secrets_roundtrips_via_string() -> None:
    """The write path's base64 str must decrypt back to the plaintext.

    Regression for the review finding: `encrypt_stored_secret(...).decode()`
    stores a `gAAAA...` str in config_json but the read path returned every str
    unchanged, so hmac_signing_secret/signing_secret were ciphertext at runtime.
    """
    config = {"hmac_secret": "shared-secret", "mode": "on_event"}
    stored = _encrypt_trigger_config_secrets(config, _FERNET_KEY)
    assert stored["hmac_secret"] != "shared-secret"
    assert isinstance(stored["hmac_secret"], str)
    assert stored["hmac_secret"].startswith("gAAAA")
    decoded = decode_stored_secret(stored["hmac_secret"], _FERNET_KEY)
    assert decoded == "shared-secret"
    assert stored["mode"] == "on_event"


def test_encrypt_trigger_config_secrets_is_idempotent() -> None:
    """Re-encrypting an already-encrypted value must not double-wrap it."""
    stored = _encrypt_trigger_config_secrets({"hmac_secret": "shared-secret"}, _FERNET_KEY)
    stored_once = stored["hmac_secret"]
    re_encrypted = _encrypt_trigger_config_secrets({"hmac_secret": stored_once}, _FERNET_KEY)
    assert re_encrypted["hmac_secret"] == stored_once
    assert decode_stored_secret(re_encrypted["hmac_secret"], _FERNET_KEY) == "shared-secret"


def test_encrypt_trigger_config_secrets_leaves_mask_and_empty_untouched() -> None:
    stored = _encrypt_trigger_config_secrets({"hmac_secret": SENSITIVE_VALUE_MASK, "signing_secret": ""}, _FERNET_KEY)
    assert stored["hmac_secret"] == SENSITIVE_VALUE_MASK
    assert not stored["signing_secret"]


def test_encrypt_trigger_config_secrets_encrypts_both_secret_keys() -> None:
    stored = _encrypt_trigger_config_secrets({"hmac_secret": "h", "signing_secret": "s"}, _FERNET_KEY)
    assert decode_stored_secret(stored["hmac_secret"], _FERNET_KEY) == "h"
    assert decode_stored_secret(stored["signing_secret"], _FERNET_KEY) == "s"


def test_encrypt_trigger_config_secrets_empty_config_returns_empty() -> None:
    assert not _encrypt_trigger_config_secrets(None, _FERNET_KEY)
    assert not _encrypt_trigger_config_secrets({}, _FERNET_KEY)
