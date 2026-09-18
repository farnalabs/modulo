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
    template_drift_fields,
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

    Delegates to the DB-layer pure function (``template_drift_fields``) so
    the drift logic has ONE implementation shared with the crud apply
    helper — the core module owns the LIVE profile-row access.
    """
    return template_drift_fields(
        provider_type=getattr(profile_row, "provider_type", None),
        image_ref=getattr(profile_row, "image_ref", None),
        network_policy=getattr(profile_row, "network_policy", None),
        persistence_policy=getattr(profile_row, "persistence_policy", None),
        config_json=getattr(profile_row, "config_json", None),
    )
