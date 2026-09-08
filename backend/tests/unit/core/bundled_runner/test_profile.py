"""Unit tests for the Bundled Runner shipped template + live drift (FAR-590 D4)."""

from types import SimpleNamespace

from modulo.core.bundled_runner import (
    BUNDLED_RUNNER_IMAGE_REF,
    BUNDLED_RUNNER_PROVIDER_TYPE,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
    build_bundled_runner_profile_values,
    template_drift_status,
)


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


def test_drift_detects_operator_pinned_older_digest() -> None:
    row = _row(image_ref="modulo-runner:opencode@sha256:" + "1" * 64)
    status = template_drift_status(row)
    assert status["drifted"] is True
    assert status["drifted_fields"] == ["image_ref"]


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
