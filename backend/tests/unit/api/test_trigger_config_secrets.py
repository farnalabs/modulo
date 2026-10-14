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


def test_encrypt_trigger_config_secrets_fails_closed_on_bad_key() -> None:
    """FAR-1732: an unusable Fernet key must abort the write — re-raise — not
    silently leave the plaintext secret intact in the returned config."""
    from modulo.auth.secret_storage import InvalidFernetKeyError

    try:
        _encrypt_trigger_config_secrets({"hmac_secret": "plaintext"}, "a" * 32)
    except InvalidFernetKeyError:
        expected_fail_closed = True
    else:
        expected_fail_closed = False
    assert expected_fail_closed


def test_encrypt_trigger_config_secrets_encrypts_gaaaa_prefixed_plaintext() -> None:
    """FAR-1735: the DECRYPTABILITY probe — not the ``gAAAA`` prefix —
    classifies ciphertext. A plaintext secret that merely begins with
    ``gAAAA`` must still be encrypted on write: the old prefix heuristic left
    such values stored in the clear, and the read path then failed closed on
    them, permanently breaking the trigger."""
    tricky_plaintext = "gAAAA-not-actually-a-fernet-token"
    stored = _encrypt_trigger_config_secrets({"hmac_secret": tricky_plaintext}, _FERNET_KEY)
    assert stored["hmac_secret"] != tricky_plaintext
    assert decode_stored_secret(stored["hmac_secret"], _FERNET_KEY) == tricky_plaintext


def test_encrypt_trigger_config_secrets_recognises_old_key_ciphertext() -> None:
    """FAR-1735: ciphertext under the ROTATION-WINDOW previous key
    (``old_fernet_key``) is recognised as already-encrypted and is never
    double-encrypted by a concurrent write during rotation."""
    old_key = Fernet.generate_key().decode()
    old_ct = Fernet(old_key.encode()).encrypt(b"shared-secret").decode()

    stored = _encrypt_trigger_config_secrets({"hmac_secret": old_ct}, _FERNET_KEY, old_fernet_key=old_key)

    assert stored["hmac_secret"] == old_ct, "old-key ciphertext must pass through untouched"
    assert decode_stored_secret(old_ct, old_key) == "shared-secret"


def test_encrypt_trigger_config_secrets_undecryptable_token_is_double_encrypted_safely() -> None:
    """A token-shaped value that NEITHER the current NOR the old key
    decrypts is treated as plaintext by the probe — so a genuinely corrupt
    stored value gets re-encrypted rather than left to fail closed on read.
    (The stored-before value is already unreadable either way; the write
    path never silently persists a NEW plaintext.)"""
    alien_ct = Fernet(Fernet.generate_key().decode()).encrypt(b"alien").decode()
    stored = _encrypt_trigger_config_secrets({"hmac_secret": alien_ct}, _FERNET_KEY)
    assert stored["hmac_secret"] != alien_ct
    assert decode_stored_secret(stored["hmac_secret"], _FERNET_KEY) == alien_ct
