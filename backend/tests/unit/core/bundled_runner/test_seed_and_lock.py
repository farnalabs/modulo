"""Unit tests for the Bundled Runner seeding + persistence lock (FAR-590 D4).

Covers the CRUD-level ephemeral-lock validator and the per-org
org-creation seeding hook (idempotent, template-valued, owned by the
org's account).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.bundled_runner.profile import (
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_PROFILE_NAME,
    build_bundled_runner_profile_values,
)
from modulo.db.crud.environment_profile import validate_runner_docker_persistence
from modulo.db.seed import seed_bundled_runner_profile

_ORG = uuid.uuid4()
_ACCOUNT = uuid.uuid4()


# ---------------------------------------------------------------------------
# Persistence lock (runner_docker -> ephemeral, everywhere)
# ---------------------------------------------------------------------------


def test_lock_rejects_retained() -> None:
    with pytest.raises(ValueError, match="locks persistence_policy"):
        validate_runner_docker_persistence("runner_docker", "retained")


def test_lock_rejects_cache() -> None:
    with pytest.raises(ValueError, match="locks persistence_policy"):
        validate_runner_docker_persistence("runner_docker", "cache")


def test_lock_allows_ephemeral() -> None:
    assert validate_runner_docker_persistence("runner_docker", "ephemeral") is None


def test_lock_ignores_other_providers() -> None:
    assert validate_runner_docker_persistence("e2b", "cache") is None
    assert validate_runner_docker_persistence("e2b", "retained") is None
    assert validate_runner_docker_persistence("local", "retained") is None


# ---------------------------------------------------------------------------
# Org-creation seeding hook
# ---------------------------------------------------------------------------


def _session(existing: bool) -> MagicMock:
    session = MagicMock()
    result = SimpleNamespace(fetchone=lambda: ("row",) if existing else None)
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


async def test_seed_inserts_template_profile_for_new_org() -> None:
    session = _session(existing=False)

    await seed_bundled_runner_profile(session, _ORG, _ACCOUNT)

    session.add.assert_called_once()
    profile = session.add.call_args.args[0]
    assert profile.organisation_id == _ORG
    assert profile.account_id == _ACCOUNT
    assert profile.name == TEMPLATE_PROFILE_NAME
    assert profile.provider_type == "runner_docker"
    assert profile.persistence_policy == "ephemeral"
    assert profile.config_json == TEMPLATE_CONFIG_JSON
    assert profile.visibility == "org"
    session.flush.assert_awaited_once()


async def test_seed_is_idempotent_when_org_already_has_one() -> None:
    session = _session(existing=True)

    await seed_bundled_runner_profile(session, _ORG, _ACCOUNT)

    session.add.assert_not_called()
    session.flush.assert_not_awaited()


async def test_seed_carries_current_template_values() -> None:
    session = _session(existing=False)

    await seed_bundled_runner_profile(session, _ORG, _ACCOUNT)

    profile = session.add.call_args.args[0]
    values = build_bundled_runner_profile_values()
    assert profile.description == values["description"]
    assert profile.image_ref == values["image_ref"]
    assert profile.capabilities_json == values["capabilities_json"]
    assert profile.network_policy == values["network_policy"]
    assert profile.initialisation_strategy == values["initialisation_strategy"]
    assert profile.secret_refs_json == values["secret_refs_json"]
