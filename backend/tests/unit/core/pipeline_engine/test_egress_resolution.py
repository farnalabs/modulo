"""Table-driven tests for the canonical egress resolution (FAR-1085).

Covers every (node_egress_policy x profile_network_policy x tier) combination,
asserting the resolved policy, the resolved allowlist, and the refusal reason.
Also includes a cert-vs-actual test ensuring derive_sandbox_capabilities'
egress capability matches the runtime resolution for every combination.
"""

from __future__ import annotations

import pytest

from modulo.core.pipeline_engine.egress import EgressResolution, resolve_egress
from modulo.core.pipeline_engine.sandbox_mode import (
    SANDBOX_CAPABILITY_EGRESS,
    derive_sandbox_capabilities,
)

_SAMPLE_ALLOWLIST: list[dict[str, str | int]] = [
    {"host": "api.example.com", "port": 443},
]


def _node_policy(policy: str | None) -> str | None:
    """Map a readable node-policy name to the raw value."""
    return None if policy == "unset" else policy


def _profile_policy(policy: str | None) -> str | None:
    """Map a readable profile-policy name to the raw value."""
    return None if policy == "unset" else policy


# Each entry: (node_raw, profile_raw, tier, has_allowlist,
#              expected_policy, expected_refusal_present, description)
_EGRESS_CASES: list[tuple[str | None, str | None, str, bool, str | None, bool, str]] = [
    # E2B: all three enforceable
    ("unset", "unset", "e2b", False, None, False, "e2b: both unset -> provider default"),
    ("unset", "outbound", "e2b", False, None, False, "e2b: profile outbound -> default"),
    ("unset", "none", "e2b", False, "deny_all", False, "e2b: profile none -> deny_all"),
    ("unset", "selected", "e2b", False, "selected", True, "e2b: profile selected w/o allowlist -> refusal"),
    ("default", "unset", "e2b", False, None, False, "e2b: node default -> provider default"),
    ("deny_all", "outbound", "e2b", False, "deny_all", False, "e2b: node deny_all overrides profile"),
    ("selected", "unset", "e2b", True, "selected", False, "e2b: node selected with allowlist"),
    ("deny_all", "none", "e2b", False, "deny_all", False, "e2b: both deny_all -> deny_all"),
    ("selected", "outbound", "e2b", False, "selected", True, "e2b: node selected no allowlist -> refusal"),
    ("unset", "unset", "e2b", False, None, False, "e2b: nothing set -> provider default"),
    # Docker: selected NOT enforceable
    ("unset", "unset", "docker", False, None, False, "docker: both unset -> provider default"),
    ("unset", "outbound", "docker", False, None, False, "docker: profile outbound -> provider default"),
    ("unset", "none", "docker", False, "deny_all", False, "docker: profile none -> deny_all"),
    ("deny_all", "outbound", "docker", False, "deny_all", False, "docker: node deny_all overrides profile"),
    ("selected", "unset", "docker", True, "selected", True, "docker: node selected -> refused"),
    ("unset", "selected", "docker", False, "selected", True, "docker: profile selected -> refused"),
    ("default", "none", "docker", False, "deny_all", False, "docker: node default, profile none -> deny_all"),
    # T3: all three enforceable
    ("unset", "unset", "t3", False, None, False, "t3: both unset -> provider default"),
    ("unset", "none", "t3", False, "deny_all", False, "t3: profile none -> deny_all"),
    ("deny_all", "outbound", "t3", False, "deny_all", False, "t3: node deny_all -> deny_all"),
    ("selected", "unset", "t3", True, "selected", False, "t3: node selected with allowlist"),
    # Local: default posture accepted (host process needs no enforcement),
    # but deny_all and selected cannot be enforced (no iptables/ns).
    ("unset", "unset", "local", False, None, False, "local: default posture accepted"),
    ("deny_all", "outbound", "local", False, "deny_all", True, "local: deny_all -> refused"),
    ("selected", "unset", "local", True, "selected", True, "local: selected -> refused"),
]


@pytest.mark.parametrize(
    ("node_raw", "profile_raw", "tier", "has_allowlist", "expected_policy", "expect_refusal", "description"),
    _EGRESS_CASES,
    ids=[c[6] for c in _EGRESS_CASES],
)
def test_resolve_egress_table(
    node_raw: str | None,
    profile_raw: str | None,
    tier: str,
    has_allowlist: bool,
    expected_policy: str | None,
    expect_refusal: bool,
    description: str,
) -> None:
    """Full table: every (node x profile x tier) -> policy, refusal."""
    node_val = _node_policy(node_raw)
    profile_val = _profile_policy(profile_raw)
    allowlist = _SAMPLE_ALLOWLIST if has_allowlist else None
    result = resolve_egress(
        node_egress_policy=node_val,
        node_egress_allowlist=allowlist,
        profile_network_policy=profile_val,
        tier=tier,
    )
    assert result.policy == expected_policy, f"{description}: expected {expected_policy!r}, got {result.policy!r}"
    if expect_refusal:
        assert result.refusal is not None, f"{description}: expected refusal"
    else:
        assert result.refusal is None, f"{description}: unexpected refusal: {result.refusal!r}"


def test_selected_without_allowlist_returns_refusal() -> None:
    """Profile selected with no allowlist from any source -> refusal."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="selected",
        tier="e2b",
    )
    assert result.policy == "selected"
    assert result.refusal is not None
    assert "allowlist" in result.refusal.lower()


def test_selected_with_node_allowlist_returns_allowlist() -> None:
    """Node selected with explicit allowlist -> resolved allowlist matches."""
    allowlist = [{"host": "api.example.com", "port": 443}]
    result = resolve_egress(
        node_egress_policy="selected",
        node_egress_allowlist=allowlist,
        profile_network_policy=None,
        tier="e2b",
    )
    assert result.policy == "selected"
    assert result.allowlist == allowlist
    assert result.refusal is None


def test_node_selected_overrides_profile_outbound() -> None:
    """Node selected wins over profile outbound -- node is explicitly set."""
    allowlist = [{"host": "api.example.com", "port": 443}]
    result = resolve_egress(
        node_egress_policy="selected",
        node_egress_allowlist=allowlist,
        profile_network_policy="outbound",
        tier="e2b",
    )
    assert result.policy == "selected"
    assert result.allowlist == allowlist
    assert result.refusal is None


def test_node_deny_all_wins_over_profile_outbound() -> None:
    """Node deny_all overrides profile outbound."""
    result = resolve_egress(
        node_egress_policy="deny_all",
        node_egress_allowlist=None,
        profile_network_policy="outbound",
        tier="e2b",
    )
    assert result.policy == "deny_all"
    assert result.refusal is None


def test_node_default_defers_to_profile() -> None:
    """Node 'default' (canonical None) defers to profile."""
    result = resolve_egress(
        node_egress_policy="default",
        node_egress_allowlist=None,
        profile_network_policy="none",
        tier="e2b",
    )
    assert result.policy == "deny_all"
    assert result.refusal is None


def test_node_unset_defers_to_profile() -> None:
    """Node unset (None) defers to profile."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="none",
        tier="e2b",
    )
    assert result.policy == "deny_all"
    assert result.refusal is None


def test_both_unset_gives_provider_default() -> None:
    """Both unset -> provider default (None)."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy=None,
        tier="e2b",
    )
    assert result.policy is None
    assert result.refusal is None


def test_docker_refuses_selected() -> None:
    """Docker tier refuses selected (no host-allowlist mechanism)."""
    result = resolve_egress(
        node_egress_policy="selected",
        node_egress_allowlist=[{"host": "api.example.com", "port": 443}],
        profile_network_policy=None,
        tier="docker",
    )
    assert result.policy == "selected"
    assert result.refusal is not None
    assert "docker" in result.refusal.lower()


def test_local_refuses_non_default_postures() -> None:
    """Local tier refuses deny_all and selected (no enforcement mechanism)."""
    for policy in ("deny_all", "selected"):
        result = resolve_egress(
            node_egress_policy=policy,
            node_egress_allowlist=([{"host": "x.com", "port": 443}] if policy == "selected" else None),
            profile_network_policy=None,
            tier="local",
        )
        assert result.refusal is not None, f"local should refuse {policy}"


def test_local_accepts_default_posture() -> None:
    """Local tier accepts provider-default posture — host process needs no enforcement."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy=None,
        tier="local",
    )
    assert result.refusal is None
    assert result.policy is None


def test_unknown_tier_returns_refusal() -> None:
    """Unknown tier returns a refusal."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy=None,
        tier="unknown_tier",
    )
    assert result.refusal is not None
    assert "unknown tier" in result.refusal.lower()


def test_profile_none_maps_to_deny_all() -> None:
    """Profile 'none' maps to canonical 'deny_all'."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="none",
        tier="e2b",
    )
    assert result.policy == "deny_all"


def test_profile_outbound_maps_to_provider_default() -> None:
    """Profile 'outbound' maps to provider default (None)."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="outbound",
        tier="e2b",
    )
    assert result.policy is None


def test_profile_selected_maps_to_selected() -> None:
    """Profile 'selected' maps to canonical 'selected'."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="selected",
        tier="e2b",
    )
    assert result.policy == "selected"


def test_node_default_maps_to_provider_default() -> None:
    """Node 'default' maps to provider default (None)."""
    result = resolve_egress(
        node_egress_policy="default",
        node_egress_allowlist=None,
        profile_network_policy=None,
        tier="e2b",
    )
    assert result.policy is None


def test_node_deny_all_maps_to_deny_all() -> None:
    """Node 'deny_all' maps to canonical 'deny_all'."""
    result = resolve_egress(
        node_egress_policy="deny_all",
        node_egress_allowlist=None,
        profile_network_policy=None,
        tier="e2b",
    )
    assert result.policy == "deny_all"


def test_node_selected_maps_to_selected() -> None:
    """Node 'selected' maps to canonical 'selected'."""
    result = resolve_egress(
        node_egress_policy="selected",
        node_egress_allowlist=[{"host": "x.com", "port": 443}],
        profile_network_policy=None,
        tier="e2b",
    )
    assert result.policy == "selected"


# --- Regression cases ---


def test_regression_profile_selected_on_docker_is_refused() -> None:
    """Regression: profile 'selected' on Docker -> refused (FAR-1083)."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="selected",
        tier="docker",
    )
    assert result.policy == "selected"
    assert result.refusal is not None


def test_regression_profile_none_node_unset_on_e2b_resolves_to_deny_all() -> None:
    """Regression: profile 'none' node unset on E2B -> deny_all (FAR-1084)."""
    result = resolve_egress(
        node_egress_policy=None,
        node_egress_allowlist=None,
        profile_network_policy="none",
        tier="e2b",
    )
    assert result.policy == "deny_all"
    assert result.refusal is None


# --- Cert-vs-actual: derive_sandbox_capabilities egress == resolve_egress ---


_CERT_CASES: list[tuple[str | None, str | None, str]] = [
    (_np, _pp, f"cert: node={_np or 'unset'}, profile={_pp or 'unset'}")
    for _np in (None, "default", "deny_all", "selected")
    for _pp in (None, "outbound", "none")
]


@pytest.mark.parametrize(
    ("node_egress", "profile_network", "description"),
    _CERT_CASES,
    ids=[c[2] for c in _CERT_CASES],
)
def test_cert_vs_actual_egress_capability(
    node_egress: str | None,
    profile_network: str | None,
    description: str,
) -> None:
    """Certified sandbox.egress must match the runtime outcome."""
    node_def: dict[str, object] = {"node_type": "sandbox_agent"}
    if node_egress is not None:
        node_def["egress_policy"] = node_egress
    if node_egress == "selected":
        node_def["egress_allowlist"] = [{"host": "x.com", "port": 443}]

    allowlist_for_runtime = [{"host": "x.com", "port": 443}] if node_egress == "selected" else None
    runtime = resolve_egress(
        node_egress_policy=node_egress,
        node_egress_allowlist=allowlist_for_runtime,
        profile_network_policy=profile_network,
        tier="e2b",
    )
    caps = derive_sandbox_capabilities(node_def, profile_network_policy=profile_network)
    certified = caps.get(SANDBOX_CAPABILITY_EGRESS)

    if runtime.refusal is not None:
        assert certified is None, f"{description}: refused but certified={certified!r}"
    elif runtime.policy is None:
        assert certified is True, f"{description}: None policy but certified={certified!r}"
    else:
        assert certified is False, f"{description}: policy={runtime.policy!r} but certified={certified!r}"


def test_non_sandbox_node_returns_empty_caps() -> None:
    """Non-sandbox_agent nodes return empty capability profile."""
    caps = derive_sandbox_capabilities({"node_type": "standard"})
    assert caps == {}


def test_egress_resolution_is_frozen() -> None:
    """EgressResolution is a frozen dataclass."""
    r = EgressResolution(policy=None, allowlist=None, refusal=None)
    with pytest.raises(AttributeError):
        r.policy = "deny_all"  # type: ignore[misc]
