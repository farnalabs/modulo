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
    # T3 / Kubernetes: Modulo authors the NetworkPolicy manifest (declared);
    # the customer's CNI enforces it and Modulo observes nothing. The
    # Kubernetes deny-all is accepted as a DECLARED posture on this basis;
    # `selected` still refuses (the Helm-rendered policy's allowlist is
    # cluster-level and cannot express a per-node allowlist).
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


# --- Tier capability matrix (declare once) --------------------------------

# Each tier declares which *canonical* egress policies it can enforce.
# ``True`` means the tier supports enforcement; ``False`` means it cannot
# and will return a ``refusal`` instead.
_TIER_ENFORCEMENT: dict[str, dict[str, bool]] = {
    "e2b": {"default": True, "deny_all": True, "selected": True},
    "docker": {"default": True, "deny_all": True, "selected": False},
    # ``t3`` and ``kubernetes`` are the SAME substrate (the Helm-rendered
    # NetworkPolicy authored by Modulo and enforced by the customer's CNI), so
    # they declare the same capability set: a ``deny_all`` declaration is
    # accepted as a DECLARED posture, while ``selected`` is a NAMED refusal —
    # the rendered policy's allowlist is cluster-level and cannot express a
    # per-node allowlist.  Neither tier name may accept ``selected`` without
    # an enforcement behind it (FAR-2068).
    "t3": {"default": True, "deny_all": True, "selected": False},
    # Local is a host process: it can provide the unrestricted default
    # posture (no enforcement needed — there is nothing to enforce), but
    # it CANNOT enforce deny_all or selected (no iptables, no network
    # namespace isolation).  Refusing default would make the local fallback
    # tier impossible to run for zero security gain.
    "local": {"default": True, "deny_all": False, "selected": False},
    # Kubernetes (FAR-1051): the pod's network is owned by the cluster CNI and
    # bounded egress is deliberately NOT ours — it is the CUSTOMER's
    # NetworkPolicy on the workspace namespace (opt-in, needs an enforcing
    # CNI).  The provider therefore enforces nothing itself.  A ``deny_all``
    # declaration IS accepted: Modulo AUTHORS the Helm-rendered NetworkPolicy
    # (deploy/helm/modulo/templates/networkpolicy.yaml) and records the
    # posture as DECLARED, while the customer's CNI enforces it and Modulo
    # observes no traffic.  ``selected`` remains a NAMED refusal — the
    # rendered policy's allowlist is cluster-level and cannot express a
    # per-node allowlist — never a silent downgrade, matching
    # KubernetesRuntimeProvider's typed ProviderCapabilityUnsupportedError.
    "kubernetes": {"default": True, "deny_all": True, "selected": False},
}

# Per-tier remediation appended to a capability refusal, where the fix is not
# "pick another tier" (FAR-1051). A refusal without this reads as a bare
# capability gap; with it the operator is told where the control actually
# lives. Tiers not listed here keep the historical message unchanged.
#
# ``t3`` and ``kubernetes`` are the same substrate, so both tier names carry
# the identical remediation note — a ``selected`` refusal must be as actionable
# under either name.
_KUBERNETES_EGRESS_REFUSAL_NOTE = (
    "on Kubernetes, bounded egress is the customer's NetworkPolicy on the workspace "
    "namespace (opt-in, requires an enforcing CNI) — the Helm-authored policy's allowlist "
    "is cluster-level and cannot express a per-node allowlist; the provider deliberately "
    "enforces nothing inside the pod"
)

_TIER_REFUSAL_NOTES: dict[str, str] = {
    "kubernetes": _KUBERNETES_EGRESS_REFUSAL_NOTE,
    "t3": _KUBERNETES_EGRESS_REFUSAL_NOTE,
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
    # --- Validate raw inputs against their vocabularies before mapping ---
    # An unrecognised value is a refusal (fail-closed): a typo'd or
    # directly-written policy must not silently fall back to the provider
    # default, which would grant egress.
    _node_raw = node_egress_policy.strip().lower() if isinstance(node_egress_policy, str) else node_egress_policy
    if _node_raw is not None and _node_raw not in NODE_EGRESS_POLICIES:
        return EgressResolution(
            policy=None,
            allowlist=None,
            refusal=(f"unrecognised node egress_policy {node_egress_policy!r}; valid values: {NODE_EGRESS_POLICIES}"),
            posture=EgressPosture.NONE,
        )
    _profile_raw = (
        profile_network_policy.strip().lower() if isinstance(profile_network_policy, str) else profile_network_policy
    )
    if _profile_raw is not None and _profile_raw not in PROFILE_NETWORK_POLICIES:
        return EgressResolution(
            policy=None,
            allowlist=None,
            refusal=(
                f"unrecognised profile network_policy"
                f" {profile_network_policy!r};"
                f" valid values: {PROFILE_NETWORK_POLICIES}"
            ),
            posture=EgressPosture.NONE,
        )

    canonical_node = _map_node_to_canonical(node_egress_policy)
    canonical_profile = _map_profile_to_canonical(profile_network_policy)

    # Resolve effective canonical policy: node explicit → profile → provider default.
    if canonical_node is not None:
        effective = canonical_node
    elif canonical_profile is not None:
        effective = canonical_profile
    else:
        effective = None  # provider default

    # Resolve effective allowlist: node explicit -> profile (not yet available
    # on the profile model -> None).  Allowlist only matters for "selected".
    effective_allowlist: list[dict[str, str | int]] | None = None
    if effective == "selected":
        # Prefer node's allowlist; fall through to profile (which currently
        # carries no allowlist -- verify in db/models/environment_profile.py).
        if node_egress_allowlist:
            effective_allowlist = list(node_egress_allowlist)
        else:
            # No allowlist available from any source.
            return EgressResolution(
                policy="selected",
                allowlist=None,
                refusal=(
                    "egress policy 'selected' requires a non-empty allowlist; "
                    "none available on this route (node has no egress_allowlist "
                    "and profile has no allowlist field)"
                ),
                posture=EgressPosture.NONE,
            )

    # Tier capability check — covers both named policies and the provider
    # default (None).  A tier that declares "default: False" (e.g. local)
    # cannot enforce even the provider-default posture and must refuse.
    tier_caps = _TIER_ENFORCEMENT.get(tier)
    if tier_caps is None:
        return EgressResolution(
            policy=effective,
            allowlist=effective_allowlist,
            refusal=f"unknown tier {tier!r}; cannot determine enforcement capability",
            posture=EgressPosture.NONE,
        )
    if not tier_caps.get(effective if effective is not None else "default", False):
        _note = _TIER_REFUSAL_NOTES.get(tier)
        return EgressResolution(
            policy=effective,
            allowlist=effective_allowlist,
            refusal=(
                f"tier {tier!r} cannot enforce egress policy {effective!r}; "
                f"available enforcement on this tier: "
                f"{[k for k, v in tier_caps.items() if v]}" + (f"; {_note}" if _note else "")
            ),
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
