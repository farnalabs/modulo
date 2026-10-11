"""Guard against credential-field drift between library definitions and the hub.

``definitions.py``'s ``credential_fields`` is the single source of truth for the
credential keys a connector type consumes. The hub's build path must read those
exact keys — any drift (the hub requires a key the declaration omits, or
declares a key the hub never reads) means a connector configured via its
library definition is skipped at ``initialise()``.

Parity is asserted BEHAVIOURALLY through the public surface: a connector built
with every declared credential field must register with the hub, and removing
any single declared field must produce a "Missing credential key '<key>'"
skip that NAMES the removed field. No source parsing is involved, so the
tests track the live behaviour including its error messages.

Connector types that consume the whole ``creds`` dict (multi-field auth) or
route through family/plugin paths cannot be expressed as a per-field
contract; they are pinned in ``EXCLUDED_TYPES`` with a reason, and the
whole-dict types additionally get a negative probe: removing one declared key
must NOT produce a "Missing credential key" skip (that skip signature is
reserved for the per-field construction path).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from modulo.core.connector_hub import ConnectorHub
from modulo.core.library.integrations import __all__ as integration_exports
from modulo.core.library.integrations import definitions as integration_defs
from modulo.core.secrets_backend import create_secrets_backend

from .secrets_helpers import SHARED_FERNET_KEY as _KEY
from .secrets_helpers import encrypt_credentials, encrypt_raw_str

# Connector types that read credentials via the whole creds dict (multi-field
# auth) or via the plugin/family registry paths, so they have no per-field
# credential contract in the hub. They are deliberately excluded from the
# per-field parity checks; the value is the reason so a future type is never
# silently without an account of its construction path.
EXCLUDED_TYPES: dict[str, str] = {
    "jira": "JiraConnector consumes the whole creds dict (multi-field auth), no per-field read",
    "confluence": "ConfluenceConnector consumes the whole creds dict (multi-field auth), no per-field read",
    "rest": "RestConnector consumes the whole creds dict (multi-field auth), no per-field read",
    "ci_runner": (
        "definitions connector_type 'ci_runner' is a family label; the hub builds github_actions_ci / gitlab_ci"
    ),
    "custom": (
        "custom connector types (prometheus, elastic, terraform, kubernetes, docker) build via the plugin registry"
    ),
}

# Minimal valid config per type so the builder passes its config validation.
_CONFIGS: dict[str, dict[str, Any]] = {
    "azure_pipelines": {"organization": "acme"},
    "jira": {"instance": "test.atlassian.net"},
    "confluence": {"instance": "my-domain.atlassian.net/wiki"},
    "rest": {"base_url": "https://api.example.com"},
}


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=lambda: encrypt_credentials({}))
    visibility: str = "org"
    allowed_operations: list[str] | None = None


def _definition_for(connector_type: str) -> dict[str, Any]:
    matches = [
        getattr(integration_defs, name)
        for name in integration_exports
        if getattr(integration_defs, name)["connector_type"] == connector_type
    ]
    assert len(matches) == 1, f"Expected one definition for {connector_type!r}, got {len(matches)}"
    return matches[0]


def _defined_types() -> list[str]:
    """Every distinct connector_type declared in the library definitions."""
    return sorted({getattr(integration_defs, name)["connector_type"] for name in integration_exports})


def _parity_checkable_types() -> list[str]:
    """Defined types subject to the per-field credential contract.

    Every defined type MINUS the explicitly excluded family/whole-dict types.
    A newly-added definition type automatically enters these checks.
    """
    return [ct for ct in _defined_types() if ct not in EXCLUDED_TYPES]


def _single_field_types() -> list[str]:
    """Defined parity-checkable types whose credential_fields name exactly one key.

    The bare-scalar heal contract is only defined for these: a single-field
    type wraps a bare scalar under its own credential key, so a bare-scalar
    ciphertext row must round-trip all the way into a registered connector.
    """
    return [ct for ct in _parity_checkable_types() if len(_definition_for(ct)["credential_fields"]) == 1]


@pytest.mark.parametrize("connector_type", _parity_checkable_types())
async def test_connector_configured_via_definitions_is_resolvable(connector_type: str) -> None:
    """A connector whose credentials use the definition's field names must build
    and register with the hub — i.e. not be silently skipped at initialise()."""
    definition = _definition_for(connector_type)
    creds = dict.fromkeys(definition["credential_fields"].keys(), "test-value")
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id=connector_type,
        config_json=_CONFIGS.get(connector_type, {}),
        credentials_ciphertext=encrypt_credentials(creds),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=json.dumps(creds)):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert ci.id in hub.connector_ids, (
        f"connector {connector_type!r} configured via its definition credentials was silently skipped at initialise"
    )


@pytest.mark.parametrize("connector_type", _parity_checkable_types())
async def test_definition_credential_fields_are_load_bearing(connector_type: str) -> None:
    """Removing EACH declared credential field individually must be fatal.

    With one field removed, instantiate must fail with a skip summary naming
    the missing field — proving the hub exactly consumes every declared key.
    A key the hub never reads would pass silently here. The parity test composes
    the four statements of the contract per field:
      (a) not registered, (b) in hub.skipped, (c) summary is ValueError: Missing credential key '<key>'.
    """
    definition = _definition_for(connector_type)
    all_keys = list(definition["credential_fields"].keys())
    assert all_keys, f"definition for {connector_type!r} declares no credential_fields to probe"
    for removed_key in all_keys:
        creds = dict.fromkeys(all_keys, "test-value")
        creds.pop(removed_key)
        ci = _FakeCI(
            id=uuid.uuid4(),
            connector_type_id=connector_type,
            config_json=_CONFIGS.get(connector_type, {}),
            credentials_ciphertext=encrypt_credentials(creds),
        )
        backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
        with patch.object(backend, "get_secret", return_value=json.dumps(creds)):
            hub = ConnectorHub(secrets_backend=backend)
            await hub.initialise([ci])
        summary = hub.skipped.get(ci.id)
        assert summary is not None, (
            f"removing {removed_key!r} from {connector_type!r} credentials did not skip the connector at initialise"
        )
        assert summary.startswith("ValueError: Missing credential key "), (
            f"removing {removed_key!r} from {connector_type!r} produced an unexpected skip: {summary!r}"
        )
        assert f"'{removed_key}'" in summary.split("Missing credential key")[1], (
            f"skip summary for {connector_type!r} does not name the missing field {removed_key!r}: {summary!r}"
        )


@pytest.mark.parametrize("connector_type", _single_field_types())
async def test_bare_scalar_ciphertext_heals_under_definition_key(connector_type: str) -> None:
    """FAR-496 contract from the definitions side: a single-field definition type
    wraps a bare-scalar ciphertext row under its own credential key, so the row
    instantiates rather than failing with "Missing credential key '<key>'".
    (Multi-field types intentionally do NOT heal to enumerable keys — their
    bare-scalar behaviour is pinned by the hub-side tests in test_connector_hub.py.)
    """
    bare_value = "def-pat-value-123"
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id=connector_type,
        config_json=_CONFIGS.get(connector_type, {}),
        credentials_ciphertext=encrypt_raw_str(bare_value),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = _KEY
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    summary = hub.skipped.get(ci.id)
    assert ci.id in hub.connector_ids, (
        f"bare scalar for single-field definition type {connector_type!r} failed to initialise: {summary!r}"
    )


@pytest.mark.parametrize(
    ("connector_type", "removed_key"),
    [
        ("jira", "api_token"),
        ("jira", "email"),
        ("confluence", "api_token"),
        ("confluence", "username"),
        ("rest", "api_key"),
        ("rest", "token"),
        ("rest", "username"),
        ("rest", "password"),
    ],
    ids=[
        "jira-no-api_token",
        "jira-no-email",
        "confluence-no-api_token",
        "confluence-no-username",
        "rest-no-api_key",
        "rest-no-token",
        "rest-no-username",
        "rest-no-password",
    ],
)
async def test_whole_dict_types_missing_field_does_not_look_like_bare_wrap_failure(
    connector_type: str, removed_key: str
) -> None:
    """Negative probe for whole-dict types: a missing declared key fails via their
    own construction path — never with the per-field "Missing credential key" skip
    signature. If a whole-dict type ever grows an internal per-field read, this
    flags it (reconcile it or reclassify the exclusion)."""
    definition = _definition_for(connector_type)
    creds = dict.fromkeys(definition["credential_fields"].keys(), "test-value")
    creds.pop(removed_key)
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id=connector_type,
        config_json=_CONFIGS.get(connector_type, {}),
        credentials_ciphertext=encrypt_credentials(creds),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=json.dumps(creds)):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    summary = hub.skipped.get(ci.id)
    assert summary is None or "Missing credential key" not in summary, (
        f"{connector_type!r} missing {removed_key!r} produced a per-field missing-key skip "
        f"(it is declared whole-dict): {summary!r}"
    )


def test_excluded_types_are_real_definition_types() -> None:
    """Every EXCLUDED_TYPES entry names a connector_type that actually exists in
    the definitions, with a non-empty reason. A stale exclusion (a type the
    definitions no longer declare, or an empty reason) proves the partition has
    drifted; the probe set is then not a faithful account of the definitions."""
    stale = set(EXCLUDED_TYPES) - set(_defined_types())
    assert not stale, f"EXCLUDED_TYPES names connector types no longer declared in definitions: {sorted(stale)}"
    empty_reasons = [ct for ct, reason in EXCLUDED_TYPES.items() if not reason.strip()]
    assert not empty_reasons, f"EXCLUDED_TYPES entries with empty reasons: {empty_reasons}"
