"""Bundled Runner shipped template constants (FAR-590, D4).

The single source of truth for the "Bundled Runner (Docker)" seeded
EnvironmentProfile template: provider type, pinned per-minor image digest,
hardening + network preset, and the LOCKED ``ephemeral`` persistence policy.

This module lives in the DB layer so both the org-creation seeding hook
(``db.seed``) and the one-time backfill migration can consume it without a
``db -> core`` import (import-linter contract ``db-does-not-import-core``).
The core-side ``bundled_runner.profile`` module re-exports these constants
and owns the LIVE drift helper.

Digest contract (ADR 029 / plan D4):
- ``BUNDLED_RUNNER_IMAGE_REF`` is **release-advanced**: the GHCR publish job
  (GA item) bumps it to the latest ``released-<minor>`` digest of the pinned
  modulo-runner image and asserts the value here matches the published tag
  (the digest-drift guard; migration 0189 mirrors these constants and the
  release job asserts they never diverge).
- Seeded rows carry their own copy of the template-owned fields at
  creation; drift is computed LIVE against these constants (no fingerprint
  lifecycle): a divergent row surfaces "shipped template updated - apply"
  on the Runners page. Operator-owned fields are surfaced, never silently
  re-applied.
"""

from __future__ import annotations

from typing import Any

TEMPLATE_PROFILE_NAME = "Bundled Runner (Docker)"

# The per-minor digest constant (release-advanced). Supports the CURRENT
# bundled-dessert minor + N-1; a pin advance past N-1 is a release-note item.
# GHCR publish CI is the GA item (deploy/docker/runner-opencode.Dockerfile).
BUNDLED_RUNNER_IMAGE_REF = (
    "modulo-runner:opencode@sha256:0000000000000000000000000000000000000000000000000000000000000000"
)

BUNDLED_RUNNER_PROVIDER_TYPE = "runner_docker"

# The release-advanced placeholder digest: an all-zero sha256. A seeded /
# backfilled profile whose ``image_ref`` still carries this cannot provision
# (the image does not exist in any registry) — a pipeline re-pointed onto the
# Bundled Runner would fail at container-create with an opaque pull error until
# the GHCR publish job (GA item) bumps the real pinned digest. This is a STABLE
# sentinel, intentionally independent of ``BUNDLED_RUNNER_IMAGE_REF``, which
# becomes the real released digest — so the placeholder check below keeps firing
# only while the digest is un-landed, and never false-positives on a real digo.
BUNDLED_RUNNER_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def is_placeholder_bundled_runner_image_ref(image_ref: str | None) -> bool:
    """True when ``image_ref`` still carries the un-landed placeholder digest."""
    return image_ref is not None and image_ref.endswith(BUNDLED_RUNNER_PLACEHOLDER_DIGEST)


# Template-owned fields (drift-keyed): provider, digest, hardening, and
# network defaults. Operator-owned fields (name/description beyond the
# template defaults) are NEVER part of drift.
_TEMPLATE_HARDENING: dict[str, Any] = {
    "memory_mb": 1024,
    "cpu_limit": 1.0,
    "read_only_rootfs": True,
    "tmpfs_paths": {"/home/user": "size=512m,mode=1777", "/tmp": "size=128m,mode=1777"},  # noqa: S108 # nosec B108  # NOSONAR - container-side tmpfs path (mode=1777 sticky-bit)
    "capabilities_drop": ["ALL"],
    "no_new_privileges": True,
    "user": "1001:1001",
}
_TEMPLATE_NETWORK: dict[str, Any] = {
    "workspace_network": "modulo-runner-workspace",
}

TEMPLATE_CONFIG_JSON: dict[str, Any] = {
    "template": "bundled-runner-docker",
    **_TEMPLATE_HARDENING,
    **_TEMPLATE_NETWORK,
    "timeout_seconds": 3600,
}

#: The complete shipped template of template-owned fields (drift source).
TEMPLATE_OWNED_FIELDS: dict[str, Any] = {
    "provider_type": BUNDLED_RUNNER_PROVIDER_TYPE,
    "image_ref": BUNDLED_RUNNER_IMAGE_REF,
    "network_policy": "outbound",
    "persistence_policy": "ephemeral",
    "config_json": TEMPLATE_CONFIG_JSON,
}


def build_bundled_runner_profile_values() -> dict[str, Any]:
    """Shipped template values for seeding / backfill (org-creation + migration)."""
    return {
        "name": TEMPLATE_PROFILE_NAME,
        "description": (
            "The Bundled Runner: first-party modulo-runner:opencode workspace "
            "executed on this deployment's Docker engine via the filtered "
            "socket proxy. Persistence is locked to ephemeral."
        ),
        "provider_type": BUNDLED_RUNNER_PROVIDER_TYPE,
        "image_ref": BUNDLED_RUNNER_IMAGE_REF,
        "capabilities_json": [],
        "config_json": TEMPLATE_CONFIG_JSON,
        "network_policy": "outbound",
        "initialisation_strategy": "git_clone",
        "secret_refs_json": [],
        "persistence_policy": "ephemeral",
    }


def template_drift_fields(
    *,
    provider_type: str | None,
    image_ref: str | None,
    network_policy: str | None,
    persistence_policy: str | None,
    config_json: Any,
) -> dict[str, Any]:
    """Compute template drift from RAW FIELD VALUES (DB-layer pure function).

    Returns ``{"is_seeded": bool, "drifted": bool, "drifted_fields": [..]}``.
    Lives in the DB layer so the crud apply helper (FAR-591 D5) and the
    core-side live drift helper share ONE implementation — the core
    ``template_drift_status`` delegates here.
    """
    if provider_type != BUNDLED_RUNNER_PROVIDER_TYPE:
        # Only template-shaped runner_docker rows participate in drift.
        return {"is_seeded": False, "drifted": False, "drifted_fields": []}
    drifted_fields: list[str] = []
    if (image_ref or "") != BUNDLED_RUNNER_IMAGE_REF:
        drifted_fields.append("image_ref")
    if network_policy != TEMPLATE_OWNED_FIELDS["network_policy"]:
        drifted_fields.append("network_policy")
    if persistence_policy != "ephemeral":
        drifted_fields.append("persistence_policy")
    cfg = config_json or {}
    shipped = TEMPLATE_OWNED_FIELDS["config_json"]
    for key, value in shipped.items():
        if cfg.get(key) != value:
            drifted_fields.append(f"config_json.{key}")
    return {
        "is_seeded": True,
        "drifted": bool(drifted_fields),
        "drifted_fields": drifted_fields,
    }
