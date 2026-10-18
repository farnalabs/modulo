"""Unit tests for apply_bundled_runner_template (FAR-591, D5 "apply" action).

The apply refreshes ONLY the drifted template-owned fields; operator-owned
fields (name, description, capabilities, secret refs, untouched config
keys) must survive. Per ADR 059 the image reference is placeholder-scoped:
while the row still carries the release placeholder digest (or no image) the
template owns it and an apply writes ``BUNDLED_RUNNER_IMAGE_REF``; once the
operator sets their own image it is neither a drift key nor an apply target,
so an operator-set image_ref survives the apply unchanged. Non-template rows
are rejected (None).
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
    async def test_refreshes_drifted_template_fields_preserving_operator_image(self) -> None:
        # ADR 059: image_ref is operator-owned, so apply refreshes the shipped
        # template WITHOUT resetting the operator's chosen image.
        profile = _make_profile()
        operator_image_ref = profile.image_ref
        assert operator_image_ref != BUNDLED_RUNNER_IMAGE_REF
        applied = await _apply(profile)
        assert applied.image_ref == operator_image_ref
        assert applied.network_policy == TEMPLATE_OWNED_FIELDS["network_policy"]
        assert applied.persistence_policy == "ephemeral"

    @pytest.mark.asyncio
    async def test_apply_advances_placeholder_image_to_template_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A placeholder-seeded row is refreshed to the advanced release constant.

        Ownership is placeholder-scoped, so an advanced ``BUNDLED_RUNNER_IMAGE_REF``
        must reach a row that still carries the placeholder digest (the
        documented remediation path).
        """
        advanced = "modulo-runner:opencode@sha256:" + "9" * 64
        monkeypatch.setattr(
            "modulo.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            advanced,
        )
        profile = _make_profile(image_ref="modulo-runner:opencode@sha256:" + "0" * 64)
        await _apply(profile)
        assert profile.image_ref == advanced

    @pytest.mark.asyncio
    async def test_apply_writes_template_image_when_row_has_none(self) -> None:
        """A row with no image is template-owned, so apply supplies the constant."""
        profile = _make_profile(image_ref=None)
        await _apply(profile)
        assert profile.image_ref == BUNDLED_RUNNER_IMAGE_REF

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
