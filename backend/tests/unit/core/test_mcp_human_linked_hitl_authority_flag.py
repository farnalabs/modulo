"""ADR 063 step 1 (FAR-1957): the org-wide ``mcp_human_linked_hitl_authority`` flag.

Default ON; an org admin may disable it (restoring the pre-ADR-063 blanket MCP
denial). The resolver fails CLOSED to OFF on any read error — a flag-resolution
failure must never widen the human_only auth boundary.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.feature_flags import (
    MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG,
    FeatureFlagRegistry,
    resolve_mcp_human_linked_hitl_authority,
)


class TestFlagRegistration:
    def test_flag_is_registered_community_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag(MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG)
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_is_default_on(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag(MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG)
        assert flag is not None
        assert flag.currently_active is True


class TestResolver:
    async def test_defaults_on_without_an_org_override(self) -> None:
        assert (
            await resolve_mcp_human_linked_hitl_authority(org_id=None, flag_name=MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG)
            is True
        )

    async def test_org_override_disables_it(self) -> None:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(return_value=False)
        with patch("modulo.core.feature_flags.get_registry", return_value=registry):
            enabled = await resolve_mcp_human_linked_hitl_authority(
                org_id=None, flag_name=MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG
            )
        assert enabled is False

    async def test_read_failure_fails_closed_to_off(self) -> None:
        registry = MagicMock()
        registry.resolve_flag = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("modulo.core.feature_flags.get_registry", return_value=registry):
            enabled = await resolve_mcp_human_linked_hitl_authority(
                org_id=None, flag_name=MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG
            )
        assert enabled is False
        registry.resolve_flag.assert_awaited_once()
        # The caller-supplied flag name is what gets resolved — the flag lives
        # at the gate site, not hidden in the registry module (FAR-1957).
        assert registry.resolve_flag.await_args.args[0] == MCP_HUMAN_LINKED_HITL_AUTHORITY_FLAG
        # The strict mode is what turns a failed ORG read into a raise instead
        # of a silent fall-through to the permissive tier default.
        assert registry.resolve_flag.await_args.kwargs["raise_on_error"] is True
