"""Canonical egress policy resolution (FAR-1085).

Centralises the mapping between the *profile* vocabulary
(``network_policy``: ``none`` / ``outbound`` / ``selected``) and the
*node* vocabulary (``egress_policy``: ``default`` / ``deny_all`` /
``selected``) into a single, tier-aware resolver.  Every call site that
builds an ``allow_internet_access`` flag, a ``WorkspaceSpec.egress_policy``,
or a conformance-certified ``sandbox.egress`` capability MUST route
through :func:`resolve_egress` so the certified capability and the
runtime enforcement agree for every (node, profile, tier) combination.

Dependency-light: only stdlib + typing.  This module is imported by
``bundled_runner/runner_dispatch.py`` and must NOT pull in heavy
pipeline-engine or DB modules.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

# --- Vocabulary constants (declare once, validate at the boundary) --------

PROFILE_NETWORK_POLICIES: tuple[str, ...] = ("none", "outbound", "selected")
"""Valid values for ``EnvironmentProfile.network_policy``."""

NODE_EGRESS_POLICIES: tuple[str, ...] = ("default", "deny_all", "selected")
"""Valid values for the node-level ``egress_policy`` field."""

WORKSPACE_METADATA_EGRESS_ALLOWLIST_KEY: str = "egress_allowlist"
"""``WorkspaceSpec.workspace_metadata`` key carrying the selected-mode allowlist.

JSON-encoded, matching the legacy E2B create path (``node_runner``) and the
E2B provider's documented metadata carrier.  Every mapper that builds a
``WorkspaceSpec`` from an :class:`EgressResolution` stamps under THIS key so
the two paths cannot diverge.
"""


# --- Typed egress posture (FAR-1690) --------------------------------------


class EgressPosture(StrEnum):
    """How a dispatch's resolved egress posture is *established* (FAR-1690).

    A TYPED provenance of the posture — deliberately never a boolean. The
    OWASP Q3 "enforcement outside the prompt" control set requires the
    posture to be typed so a permissive tier can never certify an
    enforcement it does not perform.

    Semantics (ADR 040 refusal-contract discipline, the FAR-1690 binding
    guardrail):

    - ``ENFORCED_BY_SUBSTRATE`` — the substrate is the enforcement point
      *and the provider proves it on the ADR 040 refusal-contract path*
      (``apply_isolation`` / provisioning raise on a failed enforcement
      step). Only a tier that raises on failure — E2B in-sandbox steps,
      Docker host network mode — may report this.
    - ``DECLARED`` — Modulo authors the posture as a manifest that a
      customer-owned substrate is expected to enforce, and Modulo does
      **not** itself observe the enforcement (the Kubernetes NetworkPolicy
      rendered from ``deploy/helm/modulo`` by the T3 chart). This is the
      substrate-authoring posture of the FAR-1047 decision.
    - ``METADATA_ONLY`` — the posture is recorded in metadata/telemetry but
      no substrate enforces it.
    - ``NONE`` — no egress posture is asserted (the unrestricted provider
      default, or a refused dispatch where nothing is in effect).

    **Every value is a Modulo-DECLARED fact, never an observation.** Modulo
    never observes the substrate's traffic, so no posture is ever reported
    as *observed*; ``ENFORCED_BY_SUBSTRATE`` is admitted only because the
    refusal-contract path proves the substrate's own enforcement step, not
    because Modulo watched the wire.
    """

    ENFORCED_BY_SUBSTRATE = "enforced_by_substrate"
    DECLARED = "declared"
    METADATA_ONLY = "metadata_only"
    NONE = "none"


# Posture for a *restrictive* resolved policy, per tier (FAR-1690). A tier
# whose provider raises on a failed enforcement step reports
# ENFORCED_BY_SUBSTRATE; a tier whose control is authored as a manifest for
# the customer's substrate to enforce reports DECLARED. Tiers with no
# restrictive capability never reach a restrictive resolution (they refuse),
# so their entry is NONE (unreachable, kept for totality).
_TIER_RESTRICTIVE_POSTURE: dict[str, EgressPosture] = {
    "e2b": EgressPosture.ENFORCED_BY_SUBSTRATE,
    "docker": EgressPosture.ENFORCED_BY_SUBSTRATE,
    # T3 / Kubernetes: the chart authors the NetworkPolicy manifest (declared);
    # the customer's CNI enforces it and Modulo observes nothing. Kubernetes
    # deny_all only reaches this posture when the chart's manifest is actually
    # deployed (FAR-2068, see ``workspace_network_policy_deployed``); otherwise
    # the resolver refuses before a posture is assigned.
    "t3": EgressPosture.DECLARED,
    "kubernetes": EgressPosture.DECLARED,
    "local": EgressPosture.NONE,
}


def _restrictive_posture_for_tier(tier: str) -> EgressPosture:
    """Return the posture a tier establishes for a *restrictive* resolved policy."""
    return _TIER_RESTRICTIVE_POSTURE.get(tier, EgressPosture.NONE)


# --- WorkspaceSpec dialect mapping ----------------------------------------


def spec_egress_for_canonical(policy: str | None) -> str:
    """Map a canonical egress policy onto the ``WorkspaceSpec.egress_policy`` dialect.

    Single owner of the canonical -> spec mapping (FAR-1050, ADR 040): every
    call site that derives ``WorkspaceSpec.egress_policy`` from an
    :class:`EgressResolution` MUST route through this helper so the profile
    and bundled-runner mappers cannot drift apart.

    Input vocabulary (:func:`resolve_egress` output): ``None`` (provider
    default), ``"deny_all"``, ``"selected"``.

    Output vocabulary — the ``WorkspaceSpec`` dialect consumed by the
    providers (``DockerRuntimeProvider._resolve_network_mode`` keys on
    ``"none"``; ``E2BRuntimeProvider._egress_allows_internet`` treats
    ``"selected"`` / ``"none"`` as deny-internet):

    - ``None``       -> ``"outbound"`` (unrestricted — the provider default)
    - ``"deny_all"`` -> ``"none"`` (the spec dialect's deny value)
    - ``"selected"`` -> ``"selected"`` (restrictive; the host:port allowlist
      rides as workspace/sandbox metadata)
    - anything else  -> ``"none"`` (fail CLOSED — an unknown policy must
      never silently grant unrestricted egress)
    """
    if policy is None:
        return "outbound"
    value = policy.strip().lower()
    if value == "deny_all":
        return "none"
    if value == "selected":
        return "selected"
    return "none"


# --- Workspace NetworkPolicy deployment signal (FAR-2068) -----------------

WORKSPACE_NETWORK_POLICY_ENV: str = "MODULO_WORKSPACE_NETWORK_POLICY"
"""Env var the T3 Helm chart sets when it renders the workspace NetworkPolicy.

The chart sets it to ``"enabled"`` only when
``workspaceNetworkPolicy.enabled=true`` — i.e. only when the deny-by-default
manifest that a Kubernetes ``deny_all`` DECLARED posture rests on actually
exists. Absent (the chart default) means Kubernetes ``deny_all`` stays
fail-closed: no posture may be declared without an enforcing manifest.
"""

_WORKSPACE_NETWORK_POLICY_ENABLED_VALUE = "enabled"


def workspace_network_policy_deployed() -> bool:
    """Whether the T3 chart's workspace-egress NetworkPolicy is deployed.

    Read at call time (never cached at import) so a chart rollout followed by
    a process restart cannot leave a stale claim. Only the exact value the
    chart injects counts; any other (or an absent) value keeps Kubernetes
    ``deny_all`` fail-closed.
    """
    value = (os.environ.get(WORKSPACE_NETWORK_POLICY_ENV) or "").strip().lower()
    return value == _WORKSPACE_NETWORK_POLICY_ENABLED_VALUE


# --- Tier capability matrix (declare once) --------------------------------

# Each tier declares which *canonical* egress policies it can enforce.
# ``True`` means the tier supports enforcement; ``False`` means it cannot
# and will return a ``refusal`` instead.
_TIER_ENFORCEMENT: dict[str, dict[str, bool]] = {
    "e2b": {"default": True, "deny_all": True, "selected": True},
    "docker": {"default": True, "deny_all": True, "selected": False},
    "t3": {"default": True, "deny_all": True, "selected": True},
    # Local is a host process: it can provide the unrestricted default
    # posture (no enforcement needed — there is nothing to enforce), but
    # it CANNOT enforce deny_all or selected (no iptables, no network
    # namespace isolation).  Refusing default would make the local fallback
    # tier impossible to run for zero security gain.
    "local": {"default": True, "deny_all": False, "selected": False},
    # Kubernetes (FAR-1051, FAR-2068): the pod's network is owned by the cluster
    # CNI. The CHART authors the NetworkPolicy manifest for deny_all (declared
    # posture); selected remains refused as the manifest cannot express a
    # host:port allowlist without a higher-level controller. The customer's CNI
    # enforces the authored policy; Modulo observes nothing. deny_all is only
    # enforceable when that manifest exists — see
    # ``workspace_network_policy_deployed`` and ``_tier_refusal``.
    "kubernetes": {"default": True, "deny_all": True, "selected": False},
}

# Per-tier remediation appended to a capability refusal, where the fix is not
# "pick another tier" (FAR-1051). A refusal without this reads as a bare
# capability gap; with it the operator is told where the control actually
# lives. Tiers not listed here keep the historical message unchanged.
_TIER_REFUSAL_NOTES: dict[str, str] = {
    "kubernetes": (
        "on Kubernetes, selected-mode egress is the customer's NetworkPolicy on the "
        "workspace namespace (opt-in, requires an enforcing CNI) — the provider "
        "authors deny_all as a declared NetworkPolicy; selected cannot be expressed"
    ),
}


def _map_profile_to_canonical(network_policy: str | None) -> str | None:
    """Map a profile ``network_policy`` value to the canonical egress vocabulary.

    Profile vocabulary → canonical:
      ``none``    → ``deny_all``
      ``outbound``→ ``None`` (provider default)
      ``selected``→ ``selected``
      ``None``    → ``None`` (provider default — no profile preference)
    """
    if network_policy is None:
        return None
    value = network_policy.strip().lower()
    if value == "none":
        return "deny_all"
    if value == "outbound":
        return None  # provider default
    if value == "selected":
        return "selected"
    # Defensive dead code: an unrecognised value should now be caught by
    # the validation guard in resolve_egress() before we get here.  This
    # branch is unreachable for any valid call path.
    return None


def _map_node_to_canonical(egress_policy: str | None) -> str | None:
    """Map a node ``egress_policy`` value to the canonical egress vocabulary.

    Node vocabulary → canonical:
      ``default`` → ``None`` (provider default — node defers to profile)
      ``deny_all``→ ``deny_all``
      ``selected``→ ``selected``
      ``None``    → ``None`` (node did not set — fall through to profile)
    """
    if egress_policy is None:
        return None
    value = egress_policy.strip().lower()
    if value == "default":
        return None
    if value == "deny_all":
        return "deny_all"
    if value == "selected":
        return "selected"
    # Defensive dead code: an unrecognised value should now be caught by
    # the validation guard in resolve_egress() before we get here.  This
    # branch is unreachable for any valid call path.
    return None


# --- Public API -----------------------------------------------------------


@dataclass(frozen=True)
class EgressResolution:
    """Resolved egress policy for a single dispatch.

    ``policy``
        The canonical egress policy to enforce: ``"deny_all"``,
        ``"selected"``, or ``None`` (provider default — internet allowed).

    ``allowlist``
        The resolved allowlist of host/port entries when ``policy ==
        "selected"``.  ``None`` otherwise.

    ``refusal``
        Non-``None`` when the resolved policy **cannot be enforced** on the
        given tier.  Carries a clear, actionable reason string naming the
        profile/node values and the alternatives.  A refusal is NEVER a
        silent downgrade and NEVER a silent grant.

    ``posture``
        The TYPED provenance of the resolved posture (:class:`EgressPosture`,
        FAR-1690) — never a boolean. ``NONE`` when nothing is asserted (the
        unrestricted provider default or a refused dispatch); otherwise
        ``ENFORCED_BY_SUBSTRATE`` / ``DECLARED`` / ``METADATA_ONLY`` naming
        *how* the posture is established. Defaults to ``NONE`` so an
        externally-constructed resolution (no posture established) stays
        honest without every call site supplying it.
    """

    policy: str | None
    allowlist: list[dict[str, str | int]] | None
    refusal: str | None
    posture: EgressPosture = EgressPosture.NONE


def _node_policy_refusal(node_egress_policy: str | None) -> str | None:
    """Return a refusal reason when the node policy is out of vocabulary."""
    raw = node_egress_policy.strip().lower() if isinstance(node_egress_policy, str) else node_egress_policy
    if raw is not None and raw not in NODE_EGRESS_POLICIES:
        return f"unrecognised node egress_policy {node_egress_policy!r}; valid values: {NODE_EGRESS_POLICIES}"
    return None


def _profile_policy_refusal(profile_network_policy: str | None) -> str | None:
    """Return a refusal reason when the profile policy is out of vocabulary."""
    raw = profile_network_policy.strip().lower() if isinstance(profile_network_policy, str) else profile_network_policy
    if raw is not None and raw not in PROFILE_NETWORK_POLICIES:
        return (
            f"unrecognised profile network_policy {profile_network_policy!r}; valid values: {PROFILE_NETWORK_POLICIES}"
        )
    return None


def _effective_canonical_policy(node_egress_policy: str | None, profile_network_policy: str | None) -> str | None:
    """Resolve the canonical policy: node explicit -> profile -> provider default."""
    canonical_node = _map_node_to_canonical(node_egress_policy)
    if canonical_node is not None:
        return canonical_node
    return _map_profile_to_canonical(profile_network_policy)


def _selected_allowlist(
    node_egress_allowlist: Sequence[dict[str, str | int]] | None,
) -> tuple[list[dict[str, str | int]] | None, str | None]:
    """Resolve the allowlist for a ``selected`` policy, or return a refusal."""
    if node_egress_allowlist:
        return list(node_egress_allowlist), None
    return (
        None,
        (
            "egress policy 'selected' requires a non-empty allowlist; "
            "none available on this route (node has no egress_allowlist "
            "and profile has no allowlist field)"
        ),
    )


def _tier_refusal(tier: str, effective: str | None) -> str | None:
    """Return a refusal reason when the tier cannot enforce the resolved policy.

    Kubernetes ``deny_all`` is additionally gated on the chart's workspace
    NetworkPolicy actually being deployed: the resolver may only report a
    DECLARED posture when the manifest it declares exists (FAR-2068). Without
    the chart's signal the pre-FAR-2068 fail-closed refusal applies.
    """
    tier_caps = _TIER_ENFORCEMENT.get(tier)
    if tier_caps is None:
        return f"unknown tier {tier!r}; cannot determine enforcement capability"
    key = effective if effective is not None else "default"
    if not tier_caps.get(key, False):
        note = _TIER_REFUSAL_NOTES.get(tier)
        return (
            f"tier {tier!r} cannot enforce egress policy {effective!r}; "
            f"available enforcement on this tier: "
            f"{[k for k, v in tier_caps.items() if v]}" + (f"; {note}" if note else "")
        )
    if tier == "kubernetes" and key == "deny_all" and not workspace_network_policy_deployed():
        return (
            "tier 'kubernetes' cannot establish an egress 'deny_all' (profile "
            "network_policy 'none') posture: the chart's workspace NetworkPolicy is not "
            "deployed (workspaceNetworkPolicy.enabled=false, so "
            f"{WORKSPACE_NETWORK_POLICY_ENV} is unset), and a DECLARED posture must have an "
            "enforcing manifest. Enable workspaceNetworkPolicy or use a policy this tier can "
            "enforce."
        )
    return None


def resolve_egress(
    *,
    node_egress_policy: str | None,
    node_egress_allowlist: Sequence[dict[str, str | int]] | None,
    profile_network_policy: str | None,
    tier: str,
) -> EgressResolution:
    """Resolve the egress policy for a single dispatch.

    **Precedence (one rule):**

    1. Node explicitly set (non-``None``, non-``"default"``) → wins.
    2. Node unset / ``"default"`` → the profile fills.
    3. Neither → provider default (``None`` — internet allowed).

    **``selected`` requires a non-empty allowlist.**  When the resolved
    policy is ``"selected"`` but no allowlist is available (neither from
    the node nor the profile — the profile model carries no allowlist
    field today), a refusal is returned.  Never a silent downgrade.

    **Tier capability check.**  A tier that cannot enforce the resolved
    policy returns ``refusal`` set with a clear, actionable reason.
    """
    refusal = _node_policy_refusal(node_egress_policy)
    if refusal is not None:
        return EgressResolution(policy=None, allowlist=None, refusal=refusal, posture=EgressPosture.NONE)

    refusal = _profile_policy_refusal(profile_network_policy)
    if refusal is not None:
        return EgressResolution(policy=None, allowlist=None, refusal=refusal, posture=EgressPosture.NONE)

    effective = _effective_canonical_policy(node_egress_policy, profile_network_policy)

    effective_allowlist: list[dict[str, str | int]] | None = None
    if effective == "selected":
        effective_allowlist, refusal = _selected_allowlist(node_egress_allowlist)
        if refusal is not None:
            return EgressResolution(
                policy="selected",
                allowlist=None,
                refusal=refusal,
                posture=EgressPosture.NONE,
            )

    refusal = _tier_refusal(tier, effective)
    if refusal is not None:
        return EgressResolution(
            policy=effective,
            allowlist=effective_allowlist,
            refusal=refusal,
            posture=EgressPosture.NONE,
        )

    return EgressResolution(
        policy=effective,
        allowlist=effective_allowlist,
        refusal=None,
        # A restrictive resolved policy carries the tier's establishment
        # posture; the unrestricted provider default asserts no posture.
        posture=(EgressPosture.NONE if effective is None else _restrictive_posture_for_tier(tier)),
    )
