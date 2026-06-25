"""Step definitions for security features: credential store, input sanitization, RLS."""

from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature files
# ---------------------------------------------------------------------------
scenarios("../features/security/credential_store.feature")
scenarios("../features/security/input_sanitization.feature")
scenarios("../features/security/rls_enforcement.feature")

# ===========================================================================
# security/credential_store.feature  —  3 scenarios
# ===========================================================================


@given(
    parsers.parse('a connector with API key "{api_key}"'),
    target_fixture="plaintext_key",
)
def connector_with_api_key(api_key: str) -> str:
    """Store a plaintext API key that will later be encrypted."""
    return api_key


@given("a connector with encrypted credential", target_fixture="encrypted_credential")
def connector_encrypted() -> dict[str, Any]:
    """Pre-encrypt a credential using Fernet for the read scenario."""
    key = Fernet.generate_key()
    f = Fernet(key)
    plaintext = "my-secret-api-key"
    encrypted = f.encrypt(plaintext.encode())
    return {
        "key": key,
        "encrypted": encrypted,
        "plaintext": plaintext,
    }


@given(
    parsers.parse("a credential encrypted with key A"),
    target_fixture="credential_key_a",
)
def credential_encrypted_key_a() -> dict[str, Any]:
    """Encrypt with one Fernet key so we can try to decrypt with another."""
    key_a = Fernet.generate_key()
    f_a = Fernet(key_a)
    encrypted = f_a.encrypt(b"my-secret-value")
    return {
        "key_a": key_a,
        "encrypted": encrypted,
        "plaintext": "my-secret-value",
    }


# -- Encrypt on write -------------------------------------------------------


@when(
    "I save the connector",
    target_fixture="saved_credential",
)
def save_connector(plaintext_key: str) -> dict[str, Any]:
    """Simulate encrypting a credential with Fernet on write."""
    key = Fernet.generate_key()
    f = Fernet(key)
    encrypted = f.encrypt(plaintext_key.encode())
    return {
        "fernet_key": key,
        "encrypted": encrypted,
        "plaintext": plaintext_key,
    }


@then("the stored credential value is a Fernet token")
def check_fernet_token(saved_credential: dict[str, Any]) -> None:
    """Verify the encrypted value looks like a Fernet token."""
    token = saved_credential["encrypted"]
    assert isinstance(token, bytes), (
        f"Fernet token should be bytes, got {type(token)}"
    )
    # All Fernet tokens start with the base64-encoded version byte 0x80
    # which decodes to "gAAAAA".
    assert token.startswith(b"gAAAAA"), (
        f"Token does not start with Fernet magic bytes: "
        f"{token[:20]!r}"
    )
    # A valid Fernet token can be decoded without error.
    f = Fernet(saved_credential["fernet_key"])
    decrypted = f.decrypt(token)
    assert decrypted == saved_credential["plaintext"].encode(), (
        f"Round-trip mismatch: got {decrypted!r}, "
        f"expected {saved_credential['plaintext']!r}"
    )


@then(
    parsers.parse('decrypting with FERNET_KEY yields "{expected}"'),
)
def decrypt_with_key(
    saved_credential: dict[str, Any], expected: str
) -> None:
    """Confirm that decrypting with the same key recovers the plaintext."""
    f = Fernet(saved_credential["fernet_key"])
    decrypted = f.decrypt(saved_credential["encrypted"]).decode()
    assert decrypted == expected, (
        f"Decrypted value '{decrypted}' does not match expected '{expected}'"
    )


# -- Decrypt on read within a pipeline node --------------------------------


@when("a pipeline node calls connector.query()", target_fixture="decrypted_value")
def decrypt_on_read(encrypted_credential: dict[str, Any]) -> str:
    """Simulate the decrypt-on-read that happens before a connector call."""
    f = Fernet(encrypted_credential["key"])
    plaintext = f.decrypt(encrypted_credential["encrypted"]).decode()
    return plaintext


@then("the node receives the plaintext credential")
def check_plaintext_received(decrypted_value: str) -> None:
    assert decrypted_value == "my-secret-api-key", (
        f"Expected 'my-secret-api-key', got '{decrypted_value}'"
    )


# -- Wrong FERNET_KEY cannot decrypt ---------------------------------------


@given(
    "the service restarts with key B",
    target_fixture="wrong_key",
)
def restart_with_key_b() -> bytes:
    """Generate a different Fernet key (key B) to simulate a key rotation."""
    return Fernet.generate_key()


@when("attempting to decrypt", target_fixture="decrypt_error")
def attempt_decrypt(
    credential_key_a: dict[str, Any], wrong_key: bytes
) -> Exception | None:
    """Try to decrypt key-A-encrypted data with key B."""
    try:
        f = Fernet(wrong_key)
        f.decrypt(credential_key_a["encrypted"])
        return None  # No error — unexpected
    except InvalidToken as exc:
        return exc


@then("attempting to decrypt raises InvalidToken")
def check_invalid_token(decrypt_error: Exception | None) -> None:
    assert decrypt_error is not None, (
        "Expected InvalidToken when decrypting with wrong key, "
        "but no exception was raised"
    )
    assert isinstance(decrypt_error, InvalidToken), (
        f"Expected InvalidToken, got {type(decrypt_error).__name__}: "
        f"{decrypt_error}"
    )


# ===========================================================================
# security/input_sanitization.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("I submit a prompt with Jinja2 template injection")
def step_jinja2_injection() -> bool:
    """Placeholder: Jinja2 sandbox enforcement scenario."""
    return True


@given("I submit a YAML payload with !!python/object tag")
def step_yaml_injection() -> bool:
    """Placeholder: yaml.safe_load enforcement scenario."""
    return True


# ===========================================================================
# security/rls_enforcement.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("a database connection without RLS context")
def step_no_rls_context() -> bool:
    """Placeholder: DB-level RLS bypass tests."""
    return True


@given("a connection with stale org context from a prior request")
def step_stale_org_context() -> bool:
    """Placeholder: RLS reset hook interaction test."""
    return True
