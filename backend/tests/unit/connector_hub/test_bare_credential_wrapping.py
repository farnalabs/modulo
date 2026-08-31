"""Unit tests for FAR-496: bare-token credential wrapping under the connector type's own key.

When a connector row was credentialed through the REST API with a bare token, the
credentials_ciphertext stores a bare scalar. On the read-side fallback in
``ConnectorHub.initialise()`` that bare scalar used to be wrapped under "api_key"
for every type, which breaks token-keyed connectors (github, linear, slack, ...)
that read a different credential key. These tests pin the type->key mapping and
the healing behaviour.
"""

import json
import uuid
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub import (
    _BARE_CRED_KEY_OVERRIDES,
    _TOKEN_CRED_TYPES,
    ConnectorHub,
    _bare_credential_key,
)
from modulo.core.secrets_backend import create_secrets_backend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_KEY = Fernet.generate_key().decode()


def _encrypt_raw(raw: str) -> bytes:
    """Encrypt a *bare* (non-JSON) plaintext, mimicking legacy REST storage of a bare token."""
    return Fernet(_KEY.encode()).encrypt(raw.encode())


def _encrypt_json(obj: dict) -> bytes:
    """Encrypt a JSON dict *as the stored plaintext* (round-trips untouched)."""
    return Fernet(_KEY.encode()).encrypt(json.dumps(obj).encode())


@dataclass
class _FakeCI:
    id: uuid.UUID
    connector_type_id: str
    config_json: dict = field(default_factory=dict)
    credentials_ciphertext: bytes = b""
    visibility: str = "org"
    allowed_operations: list[str] | None = None


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


# ---------------------------------------------------------------------------
# fallback wrapping integration (read-side heal)
# ---------------------------------------------------------------------------


async def test_initialise_bare_token_heals_github():
    """A bare token ciphertext for a token-keyed type now heals (FAR-496)."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt_raw("ghp_bare_token"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id).connector_type == ConnectorType.GITHUB


async def test_initialise_bare_token_heals_slack():
    """slack wraps a bare token under 'bot_token' (not 'api_key')."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="slack",
        credentials_ciphertext=_encrypt_raw("xoxb-bare-token"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id).connector_type == ConnectorType.SLACK


async def test_initialise_bare_token_heals_asana():
    """asana wraps a bare token under 'personal_access_token' (not 'api_key')."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="asana",
        credentials_ciphertext=_encrypt_raw("bare_pat"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id).connector_type == ConnectorType.ASANA


async def test_initialise_bare_scalar_api_key_type_unchanged():
    """api_key-keyed type keeps wrapping under 'api_key' (behaviour unchanged)."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="monday",
        credentials_ciphertext=_encrypt_raw("monday_bare_key"),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id).connector_type == ConnectorType.MONDAY


async def test_initialise_json_dict_plaintext_passes_through_untouched():
    """A JSON-dict plaintext is used verbatim (no api_key wrapper added)."""
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt_json({"token": "ghp_dict_token", "extra": "kept"}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert hub.get(ci.id).connector_type == ConnectorType.GITHUB
