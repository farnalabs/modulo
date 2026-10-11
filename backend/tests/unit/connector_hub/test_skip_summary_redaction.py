"""Hub-level skip-summary redaction pinning (FAR-1651Fix3).

The hub persists skip summaries (``ConnectorHub.skipped``) that callers write
into operator-visible columns (``connector_instances.last_health_check_error``
/ degraded markers). Initialisation failures can echo reflected auth headers
(basic-auth wire forms the raw credential values alone do NOT cover), so the
summary MUST be credential-scrubbed through the hub's per-instance redactor.
These tests keep the binding and the redaction from eroding.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.connectors.security import _MASK, CredentialRedactor
from modulo.core.connector_hub import _SKIP_WARN_SEEN, ConnectorHub
from modulo.core.secrets_backend import create_secrets_backend

_KEY = Fernet.generate_key().decode()

_PLANTED_TOKEN = "grhub1651plantedcredentialvalue"


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


@pytest.fixture(autouse=True)
def _reset_skip_warn_registry() -> Any:
    _SKIP_WARN_SEEN.clear()
    yield
    _SKIP_WARN_SEEN.clear()


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=lambda: _encrypt({}))
    visibility: str = "org"
    allowed_operations: list[str] | None = None


async def _initialised_hub_with_token(token: str) -> tuple[ConnectorHub, uuid.UUID]:
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id="github",
        credentials_ciphertext=_encrypt({"token": token}),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=json.dumps({"token": token})):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    return hub, ci.id


async def test_initialise_binds_redactor_for_each_instance() -> None:
    """initialise() must bind a per-instance redactor built from the decrypted creds."""
    hub, ci_id = await _initialised_hub_with_token(_PLANTED_TOKEN)
    redactor = hub.credential_redactor_for(ci_id)
    assert isinstance(redactor, CredentialRedactor)
    assert _PLANTED_TOKEN in redactor.secrets
    assert _MASK in redactor.redact(f"echo {_PLANTED_TOKEN}")
    # Unknown ids have no redactor — callers must fail closed.
    assert hub.credential_redactor_for(uuid.uuid4()) is None


async def test_record_skip_redacts_credential_echo() -> None:
    """A skip summary echoed from a credential-bearing failure must scrub the token."""
    hub, ci_id = await _initialised_hub_with_token(_PLANTED_TOKEN)
    hub._record_skip(_FakeCI(id=ci_id, connector_type_id="github"), ValueError(f"header leaked {_PLANTED_TOKEN}"))
    summary = hub.skipped[ci_id]
    assert "ValueError" in summary
    assert _PLANTED_TOKEN not in summary
    assert _MASK in summary


async def test_record_skip_clears_with_close() -> None:
    """close() drops the bound redactors together with the connector state."""
    hub, ci_id = await _initialised_hub_with_token(_PLANTED_TOKEN)
    hub.close()
    assert hub.credential_redactor_for(ci_id) is None


def test_record_skip_without_redactor_keeps_structural_summary() -> None:
    """No redactor (pre-credential failure classes) keeps the full summary.

    Documented credential-free case: exceptions raised before the creds dict
    is parsed carry only instance ids — the redactor cannot be bound because
    no plaintext ever existed. The summary is still NUL-stripped and clamped
    to the 2000-char column limit.
    """
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    hub = ConnectorHub(secrets_backend=backend)
    ci_id = uuid.uuid4()

    class _NulError(Exception):
        pass

    hub._record_skip(_FakeCI(id=ci_id, connector_type_id="github"), _NulError("boom\x00 detail"))
    summary = hub.skipped[ci_id]
    assert summary.startswith("_NulError: boom")
    assert "\x00" not in summary
    assert len(summary) <= 2000
