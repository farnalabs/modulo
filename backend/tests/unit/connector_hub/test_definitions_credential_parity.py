"""Guard against credential-field drift between library definitions and the hub.

``definitions.py``'s ``credential_fields`` is the single source of truth for the
credential keys a connector type consumes.  ``_build_connector`` must read those
exact keys via ``_get_cred`` — any drift (hub reads a key the declaration omits,
or declares a key the hub never reads) means a connector configured via its
library definition is silently skipped at ``initialise()``.

These tests assert per-connector-type parity across EVERY connector type that has
a library integration definition AND a direct ``_get_cred`` read in the hub, plus
resolvability (a connector built from its definition credentials is not skipped).
Connector types that consume the whole ``creds`` dict or route through the plugin
registry cannot be expressed as a per-field contract; they are pinned in
``EXCLUDED_TYPES`` with a reason so a future type is never silently skipped.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from modulo.core.connector_hub import ConnectorHub, _build_connector
from modulo.core.library.integrations import __all__ as integration_exports
from modulo.core.library.integrations import definitions as integration_defs
from modulo.core.secrets_backend import create_secrets_backend

# Connector types that read credentials via the whole creds dict (multi-field
# auth) or via the plugin registry, so they have no per-field ``_get_cred``
# contract in the hub. They are deliberately excluded from parity; the value is
# the reason so a future type is never silently skipped.
EXCLUDED_TYPES: dict[str, str] = {
    "jira": "JiraConnector consumes the whole creds dict (multi-field auth), no _get_cred",
    "confluence": "ConfluenceConnector consumes the whole creds dict (multi-field auth), no _get_cred",
    "rest": "RestConnector consumes the whole creds dict (multi-field auth), no _get_cred",
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
}

_KEY = Fernet.generate_key().decode()


def _encrypt(payload: dict[str, Any]) -> bytes:
    return Fernet(_KEY.encode()).encrypt(json.dumps(payload).encode())


@dataclass
class _FakeCI:
    """Minimal stand-in for ConnectorInstance (no DB needed)."""

    id: uuid.UUID
    connector_type_id: str
    config_json: dict[str, Any] = field(default_factory=dict)
    credentials_ciphertext: bytes = field(default_factory=lambda: _encrypt({}))
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


def _hub_cred_keys() -> dict[str, set[str]]:
    """Parse ``_build_connector`` to map connector_type -> the keys it reads.

    Only direct ``_get_cred(creds, "<key>", type_id)`` calls are collected;
    connectors that consume the whole ``creds`` dict (jira, confluence, rest,
    ticket-tracker) or delegate to the plugin registry ("custom") are absent
    from the mapping and not subject to a per-field contract.
    """
    source = inspect.getsource(_build_connector)
    tree = ast.parse(source)
    match_stmt: ast.Match | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Match):
            match_stmt = node
            break
    if match_stmt is None:
        return {}
    result: dict[str, set[str]] = {}
    for case in match_stmt.cases:
        if not (
            isinstance(case.pattern, ast.MatchValue)
            and isinstance(case.pattern.value, ast.Constant)
            and isinstance(case.pattern.value.value, str)
        ):
            continue
        type_id = case.pattern.value.value
        keys: set[str] = set()
        for sub in ast.walk(case):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "_get_cred"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Constant)
                and isinstance(sub.args[1].value, str)
            ):
                keys.add(sub.args[1].value)
        if keys:
            result[type_id] = keys
    return result


def _defined_types() -> list[str]:
    """Every distinct connector_type declared in the library definitions."""
    return sorted({getattr(integration_defs, name)["connector_type"] for name in integration_exports})


def _reconciled_types() -> list[str]:
    """Defined types that the hub reads via ``_get_cred`` — the parity-checkable set.

    Computed from the two sources of truth so a newly-added definition type with a
    hub ``_get_cred`` read is automatically added to the parity guard.
    """
    hub = _hub_cred_keys()
    return [ct for ct in _defined_types() if ct in hub]


@pytest.mark.parametrize("connector_type", _reconciled_types())
def test_definition_credential_keys_match_hub_read(connector_type: str) -> None:
    """Every declared credential_fields key for a connector_type must be exactly
    the keys the hub reads, so drift is caught forever."""
    definition_keys = set(_definition_for(connector_type)["credential_fields"].keys())
    hub_keys = _hub_cred_keys().get(connector_type, set())
    assert hub_keys, f"no _get_cred reads found for connector type {connector_type!r}"
    assert definition_keys == hub_keys, (
        f"credential drift for {connector_type!r}: definitions={sorted(definition_keys)} "
        f"hub=_get_cred reads {sorted(hub_keys)}"
    )


@pytest.mark.parametrize("connector_type", _reconciled_types())
async def test_connector_configured_via_definitions_is_resolvable(connector_type: str) -> None:
    """A connector whose credentials use the definition's field names must build
    and register with the hub — i.e. not be silently skipped at initialise()."""
    definition = _definition_for(connector_type)
    creds = dict.fromkeys(definition["credential_fields"].keys(), "test-value")
    ci = _FakeCI(
        id=uuid.uuid4(),
        connector_type_id=connector_type,
        config_json=_CONFIGS.get(connector_type, {}),
        credentials_ciphertext=_encrypt(creds),
    )
    backend = create_secrets_backend(fernet_key=_KEY, backend_name="fernet")
    with patch.object(backend, "get_secret", return_value=json.dumps(creds)):
        hub = ConnectorHub(secrets_backend=backend)
        await hub.initialise([ci])
    assert ci.id in hub.connector_ids, (
        f"connector {connector_type!r} configured via its definition credentials was silently skipped at initialise"
    )


@pytest.mark.parametrize("connector_type", _reconciled_types())
def test_connector_builds_from_definition_credentials(connector_type: str) -> None:
    """_build_connector resolves a connector from definition-shaped credentials."""
    definition = _definition_for(connector_type)
    creds = dict.fromkeys(definition["credential_fields"].keys(), "test-value")
    connector = _build_connector(connector_type, _CONFIGS.get(connector_type, {}), creds)
    assert connector is not None


def test_all_definition_types_are_accounted_for() -> None:
    """Every defined connector type is either parity-checked or explicitly excluded.

    Prevents a new definition type from silently escaping the parity guard.
    """
    reconciled = set(_reconciled_types())
    for ct in _defined_types():
        if ct not in reconciled:
            assert ct in EXCLUDED_TYPES, (
                f"connector type {ct!r} has a definition but no hub _get_cred read and "
                f"no reason in EXCLUDED_TYPES — reconcile it or explain the exclusion"
            )


def test_excluded_types_have_no_parity_contract() -> None:
    """No excluded type also has a direct hub _get_cred read (they must not drift)."""
    hub = _hub_cred_keys()
    overlapping = set(EXCLUDED_TYPES) & set(hub)
    assert not overlapping, (
        f"excluded types unexpectedly have a hub _get_cred read (reconcile them instead): {sorted(overlapping)}"
    )
