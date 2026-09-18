"""Unit tests for the Bundled Runner dispatch route resolution (FAR-590 D4).

Covers: the pipeline-level profile -> dispatch route resolution, the D4
upgrade rule (never-dispatch-relevant providers raise a typed config error
instead of silently activating), the locked-ephemeral persistence policy,
the loud E2B dispatch-time timeout validation (GraphValidator parity), and
the hardened WorkspaceSpec construction.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modulo.core.bundled_runner.profile import (
    is_placeholder_bundled_runner_image_ref,
)
from modulo.core.bundled_runner.runner_dispatch import (
    RunnerDispatchRoute,
    SandboxDispatchTimeoutValidationError,
    SandboxDispatchUnboundError,
    _run_broker_for,
    _workspace_spec_for_dispatch,
    resolve_sandbox_dispatch_route,
    validate_e2b_dispatch_timeout,
)

_ORG = uuid.uuid4()
_PROFILE_ID = uuid.uuid4()


def _profile(provider_type: str, **kwargs: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": _PROFILE_ID,
        "organisation_id": _ORG,
        "name": "Bundled Runner (Docker)",
        "provider_type": provider_type,
        # A realistic, non-placeholder pinned digest — the all-zero sha256 is the
        # release-advanced placeholder that cannot provision (see the Bundled
        # Runner operator guide) and is rejected at dispatch.
        "image_ref": "modulo-runner:opencode@sha256:" + "a" * 64,
        "persistence_policy": "ephemeral",
        "network_policy": "outbound",
        "capabilities_json": [],
        "config_json": {"timeout_seconds": 1800, "memory_mb": 1024, "workspace_network": "modulo-runner-workspace"},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class _SessionCM:
    """Async context manager standing in for ``session_factory()``.

    The fake session satisfies ``set_rls_org``'s preamble (active
    transaction + generic dialect) and resolves the profile on the SELECT.
    """

    def __init__(self, profile: object) -> None:
        self._session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: profile)),
            in_transaction=lambda: True,
            get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
            info={},
        )

    async def __aenter__(self) -> SimpleNamespace:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _session_factory_returning(profile: object) -> object:
    """A session_factory() async-context-manager yielding a session whose
    execute() resolves the profile (same-org enforced by the query contract)."""
    cm = _SessionCM(profile)
    return lambda: cm


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


async def test_no_bound_profile_resolves_none_route() -> None:
    route = await resolve_sandbox_dispatch_route(_session_factory_returning(None), _ORG, _PROFILE_ID)
    assert isinstance(route, RunnerDispatchRoute)
    assert route.provider_type == "none"
    assert route.profile is None
    assert route.provider is None


async def test_missing_session_factory_resolves_none_route() -> None:
    route = await resolve_sandbox_dispatch_route(None, _ORG, _PROFILE_ID)
    assert route.provider_type == "none"


async def test_e2b_profile_resolves_e2b_route() -> None:
    route = await resolve_sandbox_dispatch_route(_session_factory_returning(_profile("e2b")), _ORG, _PROFILE_ID)
    assert route.provider_type == "e2b"
    assert route.profile is not None
    assert route.provider is None


async def test_legacy_inert_local_provider_is_dispatch_unbound() -> None:
    """D4 upgrade rule: `local` was NEVER dispatch-relevant — a bound ref must
    raise the typed config error, never silently activate as dispatch reality."""
    with pytest.raises(SandboxDispatchUnboundError, match="not dispatch-relevant"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(_profile("local")), _ORG, _PROFILE_ID)


async def test_legacy_inert_local_docker_provider_is_dispatch_unbound() -> None:
    """D4 upgrade rule: `local_docker` under its old inert (conformance-only)
    semantics is dispatch-unbound — typed config error at dispatch only."""
    with pytest.raises(SandboxDispatchUnboundError, match="not dispatch-relevant"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(_profile("local_docker")), _ORG, _PROFILE_ID)


async def test_runner_docker_route_resolves_hub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the registration signal set, the hub resolves the Docker provider."""
    monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "unit-test-machine")
    route = await resolve_sandbox_dispatch_route(
        _session_factory_returning(_profile("runner_docker")), _ORG, _PROFILE_ID
    )
    try:
        assert route.provider_type == "runner_docker"
        assert route.profile is not None
        assert route.provider is not None
        assert route.hub is not None
        from modulo.core.runtime_provider.docker import DockerRuntimeProvider

        assert isinstance(route.provider, DockerRuntimeProvider)
    finally:
        if route.hub is not None:
            await route.hub.aclose()


async def test_runner_docker_without_endpoint_env_is_dispatch_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration matrix (ADR 029): with NO MODULO_RUNNER_* and no Docker
    endpoint env, `runner_docker` is not registered — the typed config error
    carries remediation copy instead of a silent fallback."""
    for var in ("MODULO_RUNNER_MACHINE_ID", "MODULO_DOCKER_HOST", "DOCKER_HOST"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SandboxDispatchUnboundError, match="MODULO_DOCKER_HOST"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(_profile("runner_docker")), _ORG, _PROFILE_ID)


async def test_unknown_provider_type_is_dispatch_unbound() -> None:
    with pytest.raises(SandboxDispatchUnboundError, match="not dispatchable"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(_profile("warp_drive")), _ORG, _PROFILE_ID)


def test_placeholder_digest_detection() -> None:
    assert is_placeholder_bundled_runner_image_ref("modulo-runner:opencode@sha256:" + "0" * 64) is True
    assert is_placeholder_bundled_runner_image_ref("modulo-runner:opencode@sha256:" + "a" * 64) is False
    assert is_placeholder_bundled_runner_image_ref(None) is False
    assert is_placeholder_bundled_runner_image_ref("") is False


async def test_runner_docker_placeholder_digest_is_dispatch_unbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-loud (don't silently break at container-create) when the profile still
    carries the release-advanced placeholder digest — a known GA follow-up."""
    monkeypatch.setenv("MODULO_RUNNER_MACHINE_ID", "unit-test-machine")
    placeholder_profile = _profile(
        "runner_docker",
        image_ref="modulo-runner:opencode@sha256:" + "0" * 64,
    )
    with pytest.raises(SandboxDispatchUnboundError, match="placeholder digest"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(placeholder_profile), _ORG, _PROFILE_ID)


# ---------------------------------------------------------------------------
# Locked-ephemeral persistence (model/crud validator parity)
# ---------------------------------------------------------------------------


async def test_runner_docker_retained_persistence_rejected_at_dispatch() -> None:
    profile = _profile("runner_docker", persistence_policy="retained")
    with pytest.raises(SandboxDispatchUnboundError, match="locks persistence_policy"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(profile), _ORG, _PROFILE_ID)


async def test_runner_docker_cache_persistence_rejected_at_dispatch() -> None:
    profile = _profile("runner_docker", persistence_policy="cache")
    with pytest.raises(SandboxDispatchUnboundError, match="locks persistence_policy"):
        await resolve_sandbox_dispatch_route(_session_factory_returning(profile), _ORG, _PROFILE_ID)


async def test_e2b_profile_keeps_non_ephemeral_persistence() -> None:
    route = await resolve_sandbox_dispatch_route(
        _session_factory_returning(_profile("e2b", persistence_policy="cache")), _ORG, _PROFILE_ID
    )
    assert route.provider_type == "e2b"


# ---------------------------------------------------------------------------
# Loud E2B dispatch-time timeout validation (no silent clamp)
# ---------------------------------------------------------------------------


def test_e2b_timeout_at_graph_validator_bound_passes() -> None:
    assert validate_e2b_dispatch_timeout(3300) is None


def test_e2b_timeout_over_bound_raises_loudly() -> None:
    with pytest.raises(SandboxDispatchTimeoutValidationError, match="3301"):
        validate_e2b_dispatch_timeout(3301)


def test_e2b_timeout_none_and_unparsable_are_noops() -> None:
    assert validate_e2b_dispatch_timeout(None) is None
    assert validate_e2b_dispatch_timeout("not-a-number") is None
    assert validate_e2b_dispatch_timeout(0) is None


# ---------------------------------------------------------------------------
# WorkspaceSpec construction
# ---------------------------------------------------------------------------


def test_workspace_spec_carries_structured_labels_and_network() -> None:
    spec = _workspace_spec_for_dispatch(
        _profile("runner_docker"),
        org_id=_ORG,
        run_id="run-123",
        node_id="node-9",
        run_uuid=uuid.uuid4(),
    )
    assert spec.workspace_metadata == {
        "modulo.run.id": "run-123",
        "modulo.org.id": str(_ORG),
        "modulo.node.id": "node-9",
    }
    assert spec.workspace_network == "modulo-runner-workspace"
    assert spec.egress_policy == "outbound"
    assert spec.image_ref.startswith("modulo-runner:opencode@sha256:")
    assert spec.resource_limits == {"memory_mb": 1024}
    assert spec.timeout_seconds == 1800


def test_workspace_spec_none_egress_opt_in_maps_to_none_policy() -> None:
    profile = _profile("runner_docker", network_policy="none")
    spec = _workspace_spec_for_dispatch(profile, org_id=_ORG, run_id="run-123", node_id="node-9", run_uuid=uuid.uuid4())
    assert spec.egress_policy == "none"


def test_workspace_spec_blank_metadata_values_dropped() -> None:
    spec = _workspace_spec_for_dispatch(
        _profile("runner_docker"), org_id=None, run_id="", node_id="node-9", run_uuid=None
    )
    assert spec.workspace_metadata == {"modulo.node.id": "node-9"}


# ---------------------------------------------------------------------------
# Run-event broker resolution (live stream publication)
# ---------------------------------------------------------------------------


def test_run_broker_for_unknown_run_returns_none() -> None:
    assert _run_broker_for("") is None
    assert _run_broker_for(str(uuid.uuid4())) is None


def test_run_broker_for_invalid_id_returns_none() -> None:
    assert _run_broker_for("not-a-uuid") is None
