"""Regression tests for FAR-1065: profile network_policy on the E2B node route.

When a sandbox_agent node's ``egress_policy`` is unset (None), the environment
profile's ``network_policy`` must be consulted. A profile with
``network_policy="none"`` must map to deny-all egress on the E2B route, even
though the node-level ``egress_policy`` is absent.

The node-level value must continue to win when explicitly set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn

_ORG_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _script_node_def(**overrides: Any) -> dict[str, Any]:
    """Minimal sandbox_agent node definition for script mode."""
    node_def: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_prompt": "ignored in script mode",
    }
    node_def.update(overrides)
    return node_def


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": str(uuid.uuid4()),
        "_pipeline_id": "pipe-1",
        "_org_id": str(_ORG_ID),
    }


def _make_sandbox_mock() -> MagicMock:
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=10))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    sandbox.get_metrics = AsyncMock(return_value=MagicMock(cpu_used_pct=1.0, mem_used=1, disk_used=1))
    return sandbox


@dataclass(frozen=True)
class _FakeProfile:
    """Minimal stand-in for an EnvironmentProfile row."""

    network_policy: str = "outbound"


@dataclass(frozen=True)
class _FakeRoute:
    """Minimal stand-in for RunnerDispatchRoute."""

    provider_type: str
    profile: Any = None


def _e2b_route(profile: Any | None = None) -> _FakeRoute:
    return _FakeRoute(provider_type="e2b", profile=profile)


def _none_route() -> _FakeRoute:
    return _FakeRoute(provider_type="none", profile=None)


_SESSION_FACTORY_MOCK = MagicMock()  # non-None session_factory to enter route resolution


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProfileNetworkPolicyEgressResolution:
    """FAR-1065: profile network_policy must be consulted when node egress_policy is unset."""

    async def test_profile_none_maps_to_deny_all(self):
        """When egress_policy is unset and profile network_policy='none', internet must be denied."""
        node_def = _script_node_def()  # egress_policy absent → None
        fn = make_sandbox_agent_fn(node_def, session_factory=_SESSION_FACTORY_MOCK)
        sandbox = _make_sandbox_mock()
        profile = _FakeProfile(network_policy="none")

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch(
                "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
                new=AsyncMock(return_value=_e2b_route(profile)),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
                return_value=None,
            ),
        ):
            await fn(_run_state())

        create_mock.assert_awaited_once()
        kwargs = create_mock.await_args.kwargs
        assert kwargs["allow_internet_access"] is False, (
            "profile network_policy='none' must deny internet when node egress_policy is unset"
        )

    async def test_profile_outbound_allows_internet(self):
        """When egress_policy is unset and profile network_policy='outbound', internet is allowed."""
        node_def = _script_node_def()
        fn = make_sandbox_agent_fn(node_def, session_factory=_SESSION_FACTORY_MOCK)
        sandbox = _make_sandbox_mock()
        profile = _FakeProfile(network_policy="outbound")

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch(
                "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
                new=AsyncMock(return_value=_e2b_route(profile)),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
                return_value=None,
            ),
        ):
            await fn(_run_state())

        create_mock.assert_awaited_once()
        assert create_mock.await_args.kwargs["allow_internet_access"] is True

    async def test_node_level_value_wins_over_profile(self, monkeypatch):
        """When node egress_policy is explicitly set, the profile is ignored."""
        monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
        node_def = _script_node_def(egress_policy="deny_all")
        fn = make_sandbox_agent_fn(node_def, session_factory=_SESSION_FACTORY_MOCK)
        sandbox = _make_sandbox_mock()
        profile = _FakeProfile(network_policy="outbound")  # profile allows, node denies

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch(
                "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
                new=AsyncMock(return_value=_e2b_route(profile)),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
                return_value=None,
            ),
        ):
            await fn(_run_state())

        create_mock.assert_awaited_once()
        assert create_mock.await_args.kwargs["allow_internet_access"] is False, (
            "node-level egress_policy must override profile network_policy"
        )

    async def test_no_profile_keeps_default_behavior(self):
        """When no profile is bound (route provider_type='none'), the default is allow."""
        node_def = _script_node_def()
        fn = make_sandbox_agent_fn(node_def, session_factory=_SESSION_FACTORY_MOCK)
        sandbox = _make_sandbox_mock()

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch(
                "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
                new=AsyncMock(return_value=_none_route()),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
                return_value=None,
            ),
        ):
            await fn(_run_state())

        create_mock.assert_awaited_once()
        assert create_mock.await_args.kwargs["allow_internet_access"] is True

    async def test_profile_selected_does_not_deny(self):
        """When egress_policy is unset and profile network_policy='selected', internet is allowed.

        The profile-level 'selected' is not a node-level concept — only 'none'
        maps to deny-all on the E2B route.
        """
        node_def = _script_node_def()
        fn = make_sandbox_agent_fn(node_def, session_factory=_SESSION_FACTORY_MOCK)
        sandbox = _make_sandbox_mock()
        profile = _FakeProfile(network_policy="selected")

        with (
            patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
            patch(
                "modulo.core.bundled_runner.runner_dispatch.resolve_sandbox_dispatch_route",
                new=AsyncMock(return_value=_e2b_route(profile)),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
                return_value=None,
            ),
        ):
            await fn(_run_state())

        create_mock.assert_awaited_once()
        assert create_mock.await_args.kwargs["allow_internet_access"] is True
