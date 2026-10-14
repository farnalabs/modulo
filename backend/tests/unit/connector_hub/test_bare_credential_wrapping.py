"""Unit tests for FAR-496: the type->credential-key mapping for bare-token wrapping.

When a connector row was credentialed through the REST API with a bare token, the
credentials_ciphertext stores a bare scalar. On the read-side fallback in
``ConnectorHub.initialise()`` that bare scalar is wrapped under the connector
type's own credential key. These tests pin the ``_bare_credential_key`` mapping;
the read-side heal path itself is exercised in ``test_connector_hub.py``.
"""

from modulo.core.connector_hub import (
    _BARE_CRED_KEY_OVERRIDES,
    _TOKEN_CRED_TYPES,
    _bare_credential_key,
)

# ---------------------------------------------------------------------------
# type -> credential key mapping
# ---------------------------------------------------------------------------


def test_bare_credential_key_token_types():
    """Every token-keyed type wraps a bare scalar under 'token'."""
    for t in sorted(_TOKEN_CRED_TYPES):
        assert _bare_credential_key(t) == "token", t


def test_bare_credential_key_overrides():
    """slack and asana wrap under their own non-token key."""
    assert _bare_credential_key("slack") == "bot_token"
    assert _bare_credential_key("asana") == "personal_access_token"
    assert _BARE_CRED_KEY_OVERRIDES == {"slack": "bot_token", "asana": "personal_access_token"}


def test_bare_credential_key_api_key_types_unchanged():
    """api_key-keyed single types keep the legacy 'api_key' default."""
    for t in ["monday", "opsgenie"]:
        assert _bare_credential_key(t) == "api_key"


def test_bare_credential_key_multikey_unchanged():
    """Multi-key types keep the legacy 'api_key' default (they need a JSON dict anyway)."""
    for t in ["jira", "datadog", "rest", "confluence", "trello", "jenkins", "ticket-tracker"]:
        assert _bare_credential_key(t) == "api_key"
