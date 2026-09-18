"""Unit tests for apply_bundled_runner_template (FAR-591, D5 "apply" action).

The apply refreshes ONLY the drifted template-owned fields; operator-owned
fields (name, description, capabilities, secret refs, untouched config
keys) must survive. Non-template rows are rejected (None).
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.bundled_runner_template import (
    BUNDLED_RUNNER_IMAGE_REF,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
)
from modulo.db.crud.environment_profile import apply_bundled_runner_template

_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.flush = AsyncMock()
    return session


def _make_profile(**overrides: Any) -> MagicMock:
    profile = MagicMock()
    profile.id = _PROFILE_ID
    profile.provider_type = overrides.get("provider_type", "runner_docker")
    profile.image_ref = overrides.get("image_ref", "modulo-runner:old@sha256:" + "2" * 64)
    profile.network_policy = overrides.get("network_policy", "none")
    profile.persistence_policy = overrides.get("persistence_policy", "ephemeral")
    profile.name = overrides.get("name", "Bundled Runner (Docker)")
    profile.description = "operator description"
    profile.capabilities_json = ["docker"]
    config = {
        "timeout_seconds": 7200,
        "memory_mb": 4096,
        "cpu_limit": 2.0,
    }
    config.update(overrides.get("config_extra", {}))
    profile.config_json = overrides.get("config_json", config)
    profile.secret_refs_json = ["vault://x"]
    return profile


async def _apply(profile: Any) -> Any:
    session = _make_session()
    with patch("modulo.db.crud.environment_profile.get_environment_profile", new=AsyncMock(return_value=profile)):
        return await apply_bundled_runner_template(session, _PROFILE_ID)


class TestApplyBundledRunnerTemplate:
    @pytest.mark.asyncio
    async def test_refreshes_drifted_template_fields(self) -> None:
        profile = _make_profile()
        applied = await _apply(profile)
        assert applied.image_ref == BUNDLED_RUNNER_IMAGE_REF
        assert applied.network_policy == TEMPLATE_OWNED_FIELDS["network_policy"]
        assert applied.persistence_policy == "ephemeral"

    @pytest.mark.asyncio
    async def test_config_drifted_keys_reset_operator_keys_survive(self) -> None:
        """Only drifted config keys reset; operator-owned keys are untouched."""
        profile = _make_profile(config_extra={"operator_note": "keep me"})
        assert profile.config_json["memory_mb"] == 4096
        assert profile.config_json["operator_note"] == "keep me"
        await _apply(profile)
        cfg = profile.config_json
        assert cfg["memory_mb"] == TEMPLATE_CONFIG_JSON["memory_mb"]
        assert cfg["cpu_limit"] == TEMPLATE_CONFIG_JSON["cpu_limit"]
        assert cfg["operator_note"] == "keep me"

    @pytest.mark.asyncio
    async def test_operator_owned_fields_survive(self) -> None:
        profile = _make_profile()
        await _apply(profile)
        assert profile.name == "Bundled Runner (Docker)"
        assert profile.description == "operator description"
        assert profile.capabilities_json == ["docker"]
        assert profile.secret_refs_json == ["vault://x"]

    @pytest.mark.asyncio
    async def test_clean_row_is_unchanged_in_shape(self) -> None:
        """Applying to a non-drifted row is a no-op on every template field."""
        profile = _make_profile(
            image_ref=BUNDLED_RUNNER_IMAGE_REF,
            network_policy="outbound",
            config_json=dict(TEMPLATE_CONFIG_JSON),
        )
        await _apply(profile)
        assert profile.image_ref == BUNDLED_RUNNER_IMAGE_REF
        assert profile.network_policy == "outbound"
        assert profile.config_json == dict(TEMPLATE_CONFIG_JSON)

    @pytest.mark.asyncio
    async def test_non_template_row_returns_none(self) -> None:
        profile = _make_profile(provider_type="e2b")
        applied = await _apply(profile)
        assert applied is None

    @pytest.mark.asyncio
    async def test_missing_row_returns_none(self) -> None:
        session = _make_session()
        with patch("modulo.db.crud.environment_profile.get_environment_profile", new=AsyncMock(return_value=None)):
            applied = await apply_bundled_runner_template(session, _PROFILE_ID)
        assert applied is None
