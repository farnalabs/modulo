"""Unit tests for modulo.api.models.error_forwarder_config.

QA lens pass (correctness, bugs, maintainability, deps) on the forwarder-config
schemas. The API routes are exercised by ``tests/unit/api/test_error_forwarder_config.py``;
this file locks the model-level contracts it leaves indirect: the full
``_mask_sensitive`` key matrix (and its non-destructive behaviour for unknown
keys), ``ForwarderConfigResponse.from_orm_model`` projection, and the update /
test-connection / list schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modulo.api.models.error_forwarder_config import (
    ForwarderConfigResponse,
    ForwarderConfigUpdate,
    ForwarderListItem,
    ForwarderListResponse,
    ForwarderTestResult,
    _mask_sensitive,
)
from modulo.api.models.error_forwarder_config import TestConnectionRequest as ConnectionRequestModel


class TestMaskSensitive:
    def test_none_config_returns_empty_dict(self) -> None:
        assert not _mask_sensitive(None)

    def test_empty_config_returns_empty_dict(self) -> None:
        assert not _mask_sensitive({})

    @pytest.mark.parametrize("key", ["dsn", "api_key", "access_token", "routing_key", "secret"])
    def test_sensitive_keys_are_masked(self, key: str) -> None:
        assert _mask_sensitive({key: "super-secret-value"}) == {key: "••••••"}

    def test_non_sensitive_keys_are_preserved(self) -> None:
        config = {"site": "datadoghq.com", "host": "logs.example.com", "port": 8080}
        assert _mask_sensitive(config) == config

    def test_mixed_config_masks_only_sensitive_keys(self) -> None:
        result = _mask_sensitive({"dsn": "https://secret", "site": "datadoghq.com"})
        assert result["dsn"] == "••••••"
        assert result["site"] == "datadoghq.com"

    def test_original_dict_is_not_mutated(self) -> None:
        config = {"dsn": "https://secret", "site": "datadoghq.com"}
        _mask_sensitive(config)
        assert config["dsn"] == "https://secret"


class TestForwarderConfigResponse:
    def test_from_orm_model_projects_all_fields(self) -> None:
        orm = _make_orm_config(
            forwarder_type="datadog",
            enabled=True,
            config_json={"dsn": "https://secret", "site": "datadoghq.com"},
            last_test_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            last_test_ok=True,
        )
        resp = ForwarderConfigResponse.from_orm_model(orm)
        assert resp.forwarder_type == "datadog"
        assert resp.enabled is True
        assert resp.config_summary["dsn"] == "••••••"
        assert resp.config_summary["site"] == "datadoghq.com"
        assert resp.last_test_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert resp.last_test_ok is True

    def test_from_orm_model_none_config_and_dates(self) -> None:
        orm = _make_orm_config(
            forwarder_type="slack",
            enabled=False,
            config_json=None,
            last_test_at=None,
            last_test_ok=None,
        )
        resp = ForwarderConfigResponse.from_orm_model(orm)
        assert not resp.config_summary
        assert resp.last_test_at is None
        assert resp.last_test_ok is None

    def test_from_orm_model_masks_all_sensitive_keys(self) -> None:
        orm = _make_orm_config(
            forwarder_type="pagerduty",
            enabled=True,
            config_json={"routing_key": "rk", "api_key": "ak", "access_token": "at", "secret": "s", "safe": "v"},
        )
        resp = ForwarderConfigResponse.from_orm_model(orm)
        assert resp.config_summary == {
            "routing_key": "••••••",
            "api_key": "••••••",
            "access_token": "••••••",
            "secret": "••••••",
            "safe": "v",
        }


class TestForwarderConfigUpdate:
    def test_both_fields_optional_and_none_by_default(self) -> None:
        update = ForwarderConfigUpdate()
        assert update.enabled is None
        assert update.config_json is None

    def test_partial_update(self) -> None:
        update = ForwarderConfigUpdate(enabled=False)
        assert update.enabled is False
        assert update.config_json is None


class TestForwarderListItem:
    def test_round_trip(self) -> None:
        item = ForwarderListItem(
            forwarder_type="datadog",
            display_name="Datadog",
            enabled=True,
            configured=True,
            last_test_at=None,
            last_test_ok=None,
        )
        assert item.forwarder_type == "datadog"
        assert item.display_name == "Datadog"
        assert item.enabled is True
        assert item.configured is True

    def test_optional_test_fields(self) -> None:
        item = ForwarderListItem(
            forwarder_type="slack",
            display_name="Slack",
            enabled=False,
            configured=False,
        )
        assert item.last_test_at is None
        assert item.last_test_ok is None


class TestForwarderListResponse:
    def test_round_trip(self) -> None:
        item = ForwarderListItem(
            forwarder_type="datadog",
            display_name="Datadog",
            enabled=True,
            configured=True,
        )
        resp = ForwarderListResponse(forwarders=[item])
        assert len(resp.forwarders) == 1
        assert resp.forwarders[0].forwarder_type == "datadog"

    def test_empty_list(self) -> None:
        resp = ForwarderListResponse(forwarders=[])
        assert not resp.forwarders


class TestTestConnectionRequest:
    def test_config_json_defaults_to_empty_dict(self) -> None:
        req = ConnectionRequestModel()
        assert not req.config_json

    def test_config_json_round_trip(self) -> None:
        req = ConnectionRequestModel(config_json={"dsn": "https://secret"})
        assert req.config_json == {"dsn": "https://secret"}


class TestForwarderTestResult:
    def test_round_trip(self) -> None:
        result = ForwarderTestResult(ok=True, message="Connected")
        assert result.ok is True
        assert result.message == "Connected"

    def test_failure_result(self) -> None:
        result = ForwarderTestResult(ok=False, message="Could not connect")
        assert result.ok is False


def _make_orm_config(**kwargs: object) -> object:
    """Build a minimal stand-in for the ``ErrorForwarderConfig`` ORM model.

    ``ForwarderConfigResponse.from_orm_model`` only reads the attributes set
    here, so a plain namespace is a faithful double.
    """
    defaults = {
        "forwarder_type": "datadog",
        "enabled": True,
        "config_json": None,
        "last_test_at": None,
        "last_test_ok": None,
    }
    defaults.update(kwargs)
    return type("_ORMConfig", (), defaults)
