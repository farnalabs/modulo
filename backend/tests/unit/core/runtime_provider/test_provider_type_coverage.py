"""Startup-assertion contract: every provider_type CHECK value is resolvable (FAR-587).

D2 of the Agent Execution Tiers plan requires that every value allowed by the
``ck_env_profiles_provider_type`` CHECK constraint either has a registered
provider or documented unconfigured behaviour (the env var whose presence
registers it, surfaced through ``ProviderNotConfiguredError``). This pins the
wider vocabulary so a future CHECK value cannot silently ship without a
provider mapping.

The vocabulary itself has a single source of truth — ``PROVIDER_TYPES`` in
``modulo.db.models.environment_profile`` (FAR-595). This module derives its
expectation from that constant and asserts the CHECK text stays in parity
with it (CHECK <-> constant <-> scanner).
"""

import re

import pytest

from modulo.core.runtime_provider import (
    _PROVIDER_ENV_VARS,
    build_hub,
    env_var_for_provider_type,
)
from modulo.db.models.environment_profile import PROVIDER_TYPES, EnvironmentProfile

# Derived from the model constant (FAR-595): the scanner expectation is no
# longer an independent hardcoded list. New vocabulary members must be added
# to PROVIDER_TYPES — which also feeds this parametrization and the env-var
# mapping test below, so an unmapped member fails here loudly.
_EXPECTED_VOCABULARY = set(PROVIDER_TYPES)


def _check_values() -> set[str]:
    constraint = next(
        c for c in EnvironmentProfile.__table_args__ if getattr(c, "name", None) == "ck_env_profiles_provider_type"
    )
    sqltext = str(getattr(constraint, "sqltext", constraint))
    return set(re.findall(r"'([a-z0-9_]+)'", sqltext))


def test_check_constraint_matches_provider_types_constant() -> None:
    """Parity: the model CHECK vocabulary is exactly PROVIDER_TYPES (FAR-595)."""
    assert _check_values() == _EXPECTED_VOCABULARY
    assert set(PROVIDER_TYPES) == _EXPECTED_VOCABULARY


def test_provider_types_constant_is_non_empty_frozenset_of_lowercase_ids() -> None:
    assert isinstance(PROVIDER_TYPES, frozenset)
    assert PROVIDER_TYPES
    assert all(value == value.strip().lower() and value for value in PROVIDER_TYPES)


@pytest.mark.parametrize("provider_type", sorted(_EXPECTED_VOCABULARY - {"local"}))
def test_gated_provider_types_have_documented_env_var(provider_type: str) -> None:
    """local is always registered; every other CHECK value names its env var."""
    assert env_var_for_provider_type(provider_type) == _PROVIDER_ENV_VARS[provider_type]


def test_local_always_registered_and_gated_types_absent_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("MODULO_RUNNER_DOCKER_HOST", raising=False)

    hub = build_hub()

    assert hub.get("local") is not None
    assert hub.get("e2b") is None
    assert hub.get("runner_docker") is None
