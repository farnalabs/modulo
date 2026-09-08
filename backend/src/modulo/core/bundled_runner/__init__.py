"""Bundled Runner (Docker) — shipped template + drift (FAR-590, D4)."""

from modulo.core.bundled_runner.profile import (
    BUNDLED_RUNNER_IMAGE_REF,
    BUNDLED_RUNNER_PROVIDER_TYPE,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
    TEMPLATE_PROFILE_NAME,
    build_bundled_runner_profile_values,
    template_drift_status,
)

__all__ = [
    "BUNDLED_RUNNER_IMAGE_REF",
    "BUNDLED_RUNNER_PROVIDER_TYPE",
    "TEMPLATE_CONFIG_JSON",
    "TEMPLATE_OWNED_FIELDS",
    "TEMPLATE_PROFILE_NAME",
    "build_bundled_runner_profile_values",
    "template_drift_status",
]
