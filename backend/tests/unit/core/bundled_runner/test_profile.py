"""Unit tests for the Bundled Runner shipped template + live drift (FAR-590 D4)."""

from types import SimpleNamespace

import pytest

from modulo.core.bundled_runner import (
    BUNDLED_RUNNER_IMAGE_REF,
    BUNDLED_RUNNER_PROVIDER_TYPE,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
    build_bundled_runner_profile_values,
    template_drift_status,
)
from modulo.db.bundled_runner_template import BUNDLED_RUNNER_PLACEHOLDER_DIGEST


def test_template_constants_shape() -> None:
    assert BUNDLED_RUNNER_PROVIDER_TYPE == "runner_docker"
    assert BUNDLED_RUNNER_IMAGE_REF.startswith("modulo-runner:opencode@sha256:")
    assert len(BUNDLED_RUNNER_IMAGE_REF.split("@sha256:", 1)[1]) == 64
    assert TEMPLATE_OWNED_FIELDS["persistence_policy"] == "ephemeral"
    assert TEMPLATE_OWNED_FIELDS["network_policy"] == "outbound"
    # D4 hardening preset (ADR 029 config table)
    assert TEMPLATE_CONFIG_JSON["memory_mb"] == 1024
    assert TEMPLATE_CONFIG_JSON["cpu_limit"] == 1.0
    assert TEMPLATE_CONFIG_JSON["read_only_rootfs"] is True
    assert TEMPLATE_CONFIG_JSON["capabilities_drop"] == ["ALL"]
    assert TEMPLATE_CONFIG_JSON["no_new_privileges"] is True
    assert TEMPLATE_CONFIG_JSON["user"] == "1001:1001"
    assert TEMPLATE_CONFIG_JSON["workspace_network"] == "modulo-runner-workspace"
    assert TEMPLATE_CONFIG_JSON["timeout_seconds"] == 3600


def test_build_values_round_trip() -> None:
    values = build_bundled_runner_profile_values()
    assert values["name"] == "Bundled Runner (Docker)"
    assert values["provider_type"] == BUNDLED_RUNNER_PROVIDER_TYPE
    assert values["image_ref"] == BUNDLED_RUNNER_IMAGE_REF
    assert values["persistence_policy"] == "ephemeral"
    assert values["network_policy"] == "outbound"
    assert values["config_json"] == TEMPLATE_CONFIG_JSON


def _row(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "provider_type": "runner_docker",
        "image_ref": BUNDLED_RUNNER_IMAGE_REF,
        "network_policy": "outbound",
        "persistence_policy": "ephemeral",
        "config_json": dict(TEMPLATE_CONFIG_JSON),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_drift_none_for_fresh_template_row() -> None:
    status = template_drift_status(_row())
    assert status == {"is_seeded": True, "drifted": False, "drifted_fields": []}


def test_drift_ignores_operator_owned_image_ref() -> None:
    """An operator-set image_ref is not drift.

    Ownership is placeholder-scoped (the FAR-1924/FAR-1779 delivery decision,
    not an ADR rule): once the operator owns the reference (any non-placeholder
    image), it must not be reported as a "shipped template updated" state.
    """
    row = _row(image_ref="ghcr.io/acme/agent@sha256:" + "1" * 64)
    status = template_drift_status(row)
    assert status == {"is_seeded": True, "drifted": False, "drifted_fields": []}


def test_drift_detects_placeholder_image_ref_when_constant_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A placeholder-seeded row reports image_ref drift once the constant advances.

    Ownership is placeholder-scoped: while the row still carries the release
    placeholder digest the template owns the reference, so an advanced
    ``BUNDLED_RUNNER_IMAGE_REF`` surfaces as drift (and apply writes it).
    """
    advanced = "modulo-runner:opencode@sha256:" + "9" * 64
    monkeypatch.setattr(
        "modulo.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        advanced,
    )
    row = _row(image_ref=BUNDLED_RUNNER_PLACEHOLDER_DIGEST)
    status = template_drift_status(row)
    assert status["is_seeded"] is True
    assert status["drifted"] is True
    assert "image_ref" in status["drifted_fields"]


def test_drift_treats_missing_image_ref_as_template_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row with no image at all is template-owned, so it reports drift."""
    advanced = "modulo-runner:opencode@sha256:" + "9" * 64
    monkeypatch.setattr(
        "modulo.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        advanced,
    )
    status = template_drift_status(_row(image_ref=None))
    assert "image_ref" in status["drifted_fields"]


def test_seeded_profile_still_receives_template_image_ref() -> None:
    """The template supplies image_ref at SEED time only.

    Ownership is placeholder-scoped (FAR-1924/FAR-1779, not an ADR rule).
    """
    values = build_bundled_runner_profile_values()
    assert values["image_ref"] == BUNDLED_RUNNER_IMAGE_REF


def test_drift_detects_config_change() -> None:
    cfg = dict(TEMPLATE_CONFIG_JSON)
    cfg["timeout_seconds"] = 600
    status = template_drift_status(_row(config_json=cfg))
    assert status["drifted"] is True
    assert "config_json.timeout_seconds" in status["drifted_fields"]


def test_drift_detects_persistence_unlock_attempt() -> None:
    status = template_drift_status(_row(persistence_policy="retained"))
    assert "persistence_policy" in status["drifted_fields"]


def test_drift_ignores_non_template_rows() -> None:
    row = _row(provider_type="e2b")
    status = template_drift_status(row)
    assert status == {"is_seeded": False, "drifted": False, "drifted_fields": []}
