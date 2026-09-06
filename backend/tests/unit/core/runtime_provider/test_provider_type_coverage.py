"""Startup-assertion contract: every provider_type CHECK value is resolvable (FAR-587).

D2 of the Agent Execution Tiers plan requires that every value allowed by the
``ck_env_profiles_provider_type`` CHECK constraint either has a registered
provider or documented unconfigured behaviour (the env var whose presence
registers it, surfaced through ``ProviderNotConfiguredError``). This pins the
wider vocabulary so a future CHECK value cannot silently ship without a
provider mapping.
"""

import re

import pytest

from modulo.core.runtime_provider import (
    _PROVIDER_ENV_VARS,
    build_hub,
    env_var_for_provider_type,
)
from modulo.db.models.environment_profile import EnvironmentProfile

_EXPECTED_VOCABULARY = {"local_docker", "e2b", "local", "runner_docker"}


def _check_values() -> set[str]:
    constraint = next(
        c for c in EnvironmentProfile.__table_args__ if getattr(c, "name", None) == "ck_env_profiles_provider_type"
    )
    sqltext = str(getattr(constraint, "sqltext", constraint))
    return set(re.findall(r"'([a-z0-9_]+)'", sqltext))


def test_check_vocabulary_is_exactly_the_known_set() -> None:
    assert _check_values() == _EXPECTED_VOCABULARY


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
