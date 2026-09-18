"""Unit tests for modulo.api.models.error_notification_rule.

QA lens pass (correctness, bugs, maintainability, deps) on the notification-rule
schemas. Basic create/update validation is exercised by
``tests/unit/error_tracking/test_error_alerting.py``; this file locks the
boundary contracts the alerting tests do not cover: default values, length and
numeric constraints on both the create and update models, the update model's
independence from the create model, and response serialization.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.api.models.error_notification_rule import (
    ErrorNotificationRuleCreate,
    ErrorNotificationRuleListResponse,
    ErrorNotificationRuleResponse,
    ErrorNotificationRuleUpdate,
)


class TestErrorNotificationRuleCreate:
    def test_defaults(self) -> None:
        rule = ErrorNotificationRuleCreate(name="My Rule")
        assert rule.enabled is True
        assert rule.condition_level == "error"
        assert rule.condition_min_count == 1
        assert rule.condition_window_seconds == 300
        assert rule.action_type == "in_app"
        assert rule.webhook_url is None
        assert rule.cooldown_seconds == 300

    def test_name_max_length_accepted(self) -> None:
        rule = ErrorNotificationRuleCreate(name="x" * 100)
        assert len(rule.name) == 100

    def test_name_exceeding_max_length_rejected(self) -> None:
        with pytest.raises(ValidationError, match="String should have at most 100 characters"):
            ErrorNotificationRuleCreate(name="x" * 101)

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("condition_min_count", 0, "Input should be greater than or equal to 1"),
            ("condition_window_seconds", 0, "Input should be greater than or equal to 1"),
            ("cooldown_seconds", -1, "Input should be greater than or equal to 0"),
        ],
    )
    def test_numeric_boundaries_rejected(self, field: str, value: object, match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            ErrorNotificationRuleCreate(**{"name": "R", field: value})

    def test_cooldown_zero_accepted(self) -> None:
        rule = ErrorNotificationRuleCreate(name="R", cooldown_seconds=0)
        assert rule.cooldown_seconds == 0

    @pytest.mark.parametrize("level", ["error", "warning", "critical"])
    def test_valid_levels_accepted(self, level: str) -> None:
        assert ErrorNotificationRuleCreate(name="R", condition_level=level).condition_level == level

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValidationError, match="condition_level must be one of"):
            ErrorNotificationRuleCreate(name="R", condition_level="debug")

    @pytest.mark.parametrize("action", ["in_app", "email", "webhook"])
    def test_valid_actions_accepted(self, action: str) -> None:
        kwargs: dict[str, str] = {"name": "R", "action_type": action}
        if action == "webhook":
            kwargs["webhook_url"] = "https://hooks.example.com"
        assert ErrorNotificationRuleCreate(**kwargs).action_type == action

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(ValidationError, match="action_type must be one of"):
            ErrorNotificationRuleCreate(name="R", action_type="sms")

    def test_webhook_url_http_scheme_accepted(self) -> None:
        rule = ErrorNotificationRuleCreate(name="R", action_type="webhook", webhook_url="http://hooks.example.com")
        assert rule.webhook_url == "http://hooks.example.com"

    def test_webhook_url_non_http_rejected(self) -> None:
        with pytest.raises(ValidationError, match="webhook_url must start with"):
            ErrorNotificationRuleCreate(name="R", action_type="webhook", webhook_url="ftp://hooks.example.com")

    @pytest.mark.parametrize(
        "url",
        ["https:hooks.example.com", "https://"],
    )
    def test_webhook_url_malformed_rejected(self, url: str) -> None:
        with pytest.raises(ValidationError, match="webhook_url must start with"):
            ErrorNotificationRuleCreate(name="R", action_type="webhook", webhook_url=url)


class TestErrorNotificationRuleUpdate:
    def test_empty_update_is_all_none(self) -> None:
        update = ErrorNotificationRuleUpdate()
        assert update.model_dump() == {
            "name": None,
            "enabled": None,
            "condition_level": None,
            "condition_min_count": None,
            "condition_window_seconds": None,
            "action_type": None,
            "webhook_url": None,
            "cooldown_seconds": None,
        }

    def test_partial_update_only_sets_provided_fields(self) -> None:
        update = ErrorNotificationRuleUpdate(enabled=False)
        assert update.enabled is False
        assert update.name is None
        assert update.condition_level is None

    def test_invalid_level_rejected_on_update(self) -> None:
        with pytest.raises(ValidationError, match="condition_level must be one of"):
            ErrorNotificationRuleUpdate(condition_level="fatal")

    @pytest.mark.parametrize("level", ["error", "warning", "critical"])
    def test_valid_levels_accepted_on_update(self, level: str) -> None:
        update = ErrorNotificationRuleUpdate(condition_level=level)
        assert update.condition_level == level

    def test_invalid_action_rejected_on_update(self) -> None:
        with pytest.raises(ValidationError, match="action_type must be one of"):
            ErrorNotificationRuleUpdate(action_type="sms")

    @pytest.mark.parametrize("action", ["in_app", "email", "webhook"])
    def test_valid_actions_accepted_on_update(self, action: str) -> None:
        kwargs: dict[str, str] = {"action_type": action}
        if action == "webhook":
            kwargs["webhook_url"] = "https://hooks.example.com"
        assert ErrorNotificationRuleUpdate(**kwargs).action_type == action

    def test_webhook_url_validation_applies_on_update(self) -> None:
        with pytest.raises(ValidationError, match="webhook_url must start with"):
            ErrorNotificationRuleUpdate(webhook_url="ftp://hooks.example.com")

    def test_webhook_url_none_skips_validation(self) -> None:
        update = ErrorNotificationRuleUpdate(webhook_url=None)
        assert update.webhook_url is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "x" * 101),
            ("condition_min_count", 0),
            ("condition_window_seconds", 0),
            ("cooldown_seconds", -1),
        ],
    )
    def test_boundaries_rejected_on_update(self, field: str, value: object) -> None:
        with pytest.raises(ValidationError):
            ErrorNotificationRuleUpdate(**{field: value})

    def test_valid_boundaries_accepted_on_update(self) -> None:
        update = ErrorNotificationRuleUpdate(
            name="x" * 100,
            condition_min_count=1,
            condition_window_seconds=1,
            cooldown_seconds=0,
        )
        assert update.condition_min_count == 1
        assert update.cooldown_seconds == 0


class TestErrorNotificationRuleResponse:
    def test_round_trip(self) -> None:
        resp = ErrorNotificationRuleResponse(
            id="rule-1",
            name="Critical alerts",
            enabled=True,
            condition_level="critical",
            condition_min_count=5,
            condition_window_seconds=600,
            action_type="webhook",
            webhook_url="https://hooks.example.com/alerts",
            cooldown_seconds=900,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-02T00:00:00Z",
        )
        assert resp.id == "rule-1"
        assert resp.name == "Critical alerts"
        assert resp.webhook_url == "https://hooks.example.com/alerts"
        assert resp.cooldown_seconds == 900

    def test_webhook_url_optional(self) -> None:
        resp = ErrorNotificationRuleResponse(
            id="rule-2",
            name="In-app only",
            enabled=True,
            condition_level="error",
            condition_min_count=1,
            condition_window_seconds=300,
            action_type="in_app",
            cooldown_seconds=300,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert resp.webhook_url is None


class TestErrorNotificationRuleListResponse:
    def test_round_trip(self) -> None:
        rule = ErrorNotificationRuleResponse(
            id="rule-1",
            name="Rule",
            enabled=True,
            condition_level="error",
            condition_min_count=1,
            condition_window_seconds=300,
            action_type="in_app",
            cooldown_seconds=300,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        resp = ErrorNotificationRuleListResponse(items=[rule], total=1, limit=50, offset=0)
        assert resp.total == 1
        assert resp.limit == 50
        assert resp.offset == 0
        assert len(resp.items) == 1
        assert resp.items[0].name == "Rule"

    def test_empty_items(self) -> None:
        resp = ErrorNotificationRuleListResponse(items=[], total=0, limit=50, offset=0)
        assert not resp.items
