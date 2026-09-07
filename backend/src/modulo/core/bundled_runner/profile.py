"""Bundled Runner shipped template + live drift (FAR-590, D4).

The shipped template constants live in the DB layer
(``modulo.db.bundled_runner_template``) so the seeding hook and the backfill
migration can consume them without a ``db -> core`` import; this module
re-exports them and owns the LIVE drift helper (no fingerprint lifecycle:
drift is computed against the constants on every read).
"""

from __future__ import annotations

from typing import Any

from modulo.db.bundled_runner_template import (
    BUNDLED_RUNNER_IMAGE_REF,
    BUNDLED_RUNNER_PLACEHOLDER_DIGEST,
    BUNDLED_RUNNER_PROVIDER_TYPE,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
    TEMPLATE_PROFILE_NAME,
    build_bundled_runner_profile_values,
    is_placeholder_bundled_runner_image_ref,
)

__all__ = [
    "BUNDLED_RUNNER_IMAGE_REF",
    "BUNDLED_RUNNER_PLACEHOLDER_DIGEST",
    "BUNDLED_RUNNER_PROVIDER_TYPE",
    "TEMPLATE_CONFIG_JSON",
    "TEMPLATE_OWNED_FIELDS",
    "TEMPLATE_PROFILE_NAME",
    "build_bundled_runner_profile_values",
    "is_placeholder_bundled_runner_image_ref",
    "template_drift_status",
]


def template_drift_status(profile_row: Any) -> dict[str, Any]:
    """Compute template drift LIVE for a seeded Bundled Runner profile row.

    Returns ``{"is_seeded": bool, "drifted": bool, "drifted_fields": [..]}``.
    Only the template-owned fields are compared. No state to clear, no
    silent auto-refresh: the Runners page (D5) surfaces per-row "shipped
    template updated - apply" when ``drifted`` is True.
    """
    provider_type = getattr(profile_row, "provider_type", None)
    if provider_type != BUNDLED_RUNNER_PROVIDER_TYPE:
        # Only template-shaped runner_docker rows participate in drift.
        return {"is_seeded": False, "drifted": False, "drifted_fields": []}
    drifted_fields: list[str] = []
    if (getattr(profile_row, "image_ref", None) or "") != BUNDLED_RUNNER_IMAGE_REF:
        drifted_fields.append("image_ref")
    network_policy = getattr(profile_row, "network_policy", None)
    if network_policy != TEMPLATE_OWNED_FIELDS["network_policy"]:
        drifted_fields.append("network_policy")
    persistence = getattr(profile_row, "persistence_policy", None)
    if persistence != "ephemeral":
        drifted_fields.append("persistence_policy")
    cfg = getattr(profile_row, "config_json", None) or {}
    shipped = TEMPLATE_OWNED_FIELDS["config_json"]
    for key, value in shipped.items():
        if cfg.get(key) != value:
            drifted_fields.append(f"config_json.{key}")
    return {
        "is_seeded": True,
        "drifted": bool(drifted_fields),
        "drifted_fields": drifted_fields,
    }
