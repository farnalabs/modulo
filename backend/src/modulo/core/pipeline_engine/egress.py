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

# --- Vocabulary constants (declare once, validate at the boundary) --------

PROFILE_NETWORK_POLICIES: tuple[str, ...] = ("none", "outbound", "selected")
"""Valid values for ``EnvironmentProfile.network_policy``."""

NODE_EGRESS_POLICIES: tuple[str, ...] = ("default", "deny_all", "selected")
"""Valid values for the node-level ``egress_policy`` field."""


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
    """

    policy: str | None
    allowlist: list[dict[str, str | int]] | None
    refusal: str | None


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
        )
    if not tier_caps.get(effective if effective is not None else "default", False):
        return EgressResolution(
            policy=effective,
            allowlist=effective_allowlist,
            refusal=(
                f"tier {tier!r} cannot enforce egress policy {effective!r}; "
                f"available enforcement on this tier: "
                f"{[k for k, v in tier_caps.items() if v]}"
            ),
        )

    return EgressResolution(
        policy=effective,
        allowlist=effective_allowlist,
        refusal=None,
    )
