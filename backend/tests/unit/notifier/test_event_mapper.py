"""Unit tests for NotificationEventMapper — platform-event → in-app Notification mapping.

Covers every event category's config mapping (level/scope/category/dismiss
strategy/TTL), title/body/action-url template resolution, the unknown-event
no-op path, target_user_id passthrough, and expiry computation — all without a
DB (``create_notification`` is patched).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.notifier.event_mapper import NotificationEventMapper
from modulo.db.models.notification import Notification

_ORG = uuid.uuid4()
_TARGET_USER = uuid.uuid4()

_PAYLOAD = {
    "pipeline_name": "my-pipeline",
    "agent_name": "my-agent",
    "error_code": "E_TIMEOUT",
    "run_id": str(uuid.uuid4()),
    "minutes_overdue": 12,
    "message": "hello world",
}


@pytest.fixture
def mapper() -> NotificationEventMapper:
    return NotificationEventMapper()


def _notification_stub() -> Notification:
    return MagicMock(spec=Notification)


async def _call(
    mapper: NotificationEventMapper,
    event_type: str,
    payload: dict[str, object] | None = None,
    target_user_id: uuid.UUID | None = None,
    org_id: uuid.UUID = _ORG,
) -> tuple[Notification | None, MagicMock]:
    """Invoke create_from_event with a patched create_notification, returning (result, mock)."""
    session = AsyncMock()
    with patch(
        "modulo.core.notifier.event_mapper.create_notification",
        new_callable=AsyncMock,
        return_value=_notification_stub(),
    ) as mock_create:
        result = await mapper.create_from_event(
            session,
            org_id=org_id,
            event_type=event_type,
            payload=payload if payload is not None else dict(_PAYLOAD),
            target_user_id=target_user_id,
        )
    return result, mock_create


# ---------------------------------------------------------------------------
# Unknown event type
# ---------------------------------------------------------------------------


async def test_unknown_event_returns_none(mapper: NotificationEventMapper) -> None:
    result, mock_create = await _call(mapper, "no_such_event")
    assert result is None
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Event category config mapping
# ---------------------------------------------------------------------------

_EXPECTED_MAPPING = {
    "hitl_awaiting": ("info", "org", "hitl.awaiting", "any_scope", True, 72),
    "run_failed": ("error", "org", "run.failed", "any_scope", True, 168),
    "budget_exceeded": ("warning", "org", "run.budget_exceeded", "org_admin", True, 168),
    "claim_expired": ("info", "org", "hitl.claim_expired", "any_scope", True, 24),
    "hitl_overdue": ("warning", "admin", "hitl.overdue", "org_admin", True, 168),
    "eval_regression": ("warning", "org", "eval.regression", "any_scope", True, 336),
    "eval_blocked": ("error", "org", "eval.blocked", "any_scope", True, 168),
    "feedback_pending": ("info", "user", "feedback.pending", "user_only", False, 336),
}


@pytest.mark.parametrize(
    ("event_type", "level", "scope", "category", "dismiss_strategy", "dismissible_at_scope", "ttl_hours"),
    [(event_type, *config) for event_type, config in _EXPECTED_MAPPING.items()],
    ids=list(_EXPECTED_MAPPING),
)
async def test_known_event_maps_config(
    mapper: NotificationEventMapper,
    event_type: str,
    level: str,
    scope: str,
    category: str,
    dismiss_strategy: str,
    dismissible_at_scope: bool,
    ttl_hours: int,
) -> None:
    result, mock_create = await _call(mapper, event_type)
    assert result is not None
    mock_create.assert_awaited_once()
    kwargs = mock_create.await_args.kwargs
    assert kwargs["org_id"] == _ORG
    assert kwargs["scope"] == scope
    assert kwargs["level"] == level
    assert kwargs["category"] == category
    assert kwargs["dismiss_strategy"] == dismiss_strategy
    assert kwargs["dismissible_at_scope"] is dismissible_at_scope
    assert kwargs["target_user_id"] is None


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------


async def test_hitl_awaiting_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "hitl_awaiting")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "HITL review needed — my-pipeline"
    assert kwargs["body"] == 'Pipeline "my-pipeline" is waiting for human review.'
    assert kwargs["action_url"] == f"/runs/{_PAYLOAD['run_id']}"


async def test_run_failed_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "run_failed")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "Run failed — my-pipeline"
    assert kwargs["body"] == 'Run for "my-pipeline" failed with error: E_TIMEOUT.'
    assert kwargs["action_url"] == f"/runs/{_PAYLOAD['run_id']}"


async def test_eval_regression_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "eval_regression")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "Eval regression detected — my-agent"
    assert kwargs["body"] == 'Eval pass rate dropped for agent "my-agent".'
    assert kwargs["action_url"] == "/evals"


async def test_budget_exceeded_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "budget_exceeded")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "Budget exceeded — my-pipeline"
    assert kwargs["body"] == 'Run for "my-pipeline" exceeded its token budget.'
    assert kwargs["action_url"] == f"/runs/{_PAYLOAD['run_id']}"


async def test_claim_expired_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "claim_expired")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "HITL claim expired — my-pipeline"
    assert kwargs["body"] == 'A HITL claim on "my-pipeline" has expired.'
    assert kwargs["action_url"] == f"/runs/{_PAYLOAD['run_id']}"


async def test_eval_blocked_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "eval_blocked")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "Eval blocked — my-pipeline"
    assert kwargs["body"] == 'An eval check blocked pipeline "my-pipeline".'
    assert kwargs["action_url"] == f"/runs/{_PAYLOAD['run_id']}"


async def test_hitl_overdue_templates_resolved(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "hitl_overdue")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "HITL overdue — my-pipeline"
    assert kwargs["body"] == 'Pipeline "my-pipeline" has been awaiting human review for 12 minutes.'
    assert kwargs["action_url"] == f"/runs/{_PAYLOAD['run_id']}"


async def test_system_announcement_uses_message_payload(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "system_announcement")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "System announcement"
    assert kwargs["body"] == "hello world"
    assert kwargs["action_url"] is None


async def test_feedback_pending_static_action_url_independent_of_payload(
    mapper: NotificationEventMapper,
) -> None:
    """feedback_pending has a static action URL that does not depend on the payload."""
    _, mock_create = await _call(mapper, "feedback_pending", payload={})
    kwargs = mock_create.await_args.kwargs
    assert kwargs["action_url"] == "/feedback/inbox"
    assert kwargs["body"] == "A feedback record is pending your review."


# ---------------------------------------------------------------------------
# Template failure resilience
# ---------------------------------------------------------------------------


async def test_missing_template_key_replaced_with_unknown(mapper: NotificationEventMapper) -> None:
    _, mock_create = await _call(mapper, "hitl_awaiting", payload={})
    kwargs = mock_create.await_args.kwargs
    assert kwargs["title"] == "HITL review needed — [unknown]"
    assert kwargs["body"] == 'Pipeline "[unknown]" is waiting for human review.'


async def test_multiple_missing_keys_survive_after_first_replacement(mapper: NotificationEventMapper) -> None:
    """A payload missing two template keys must not raise after the first substitution."""
    _, mock_create = await _call(mapper, "run_failed", payload={"error_code": "E_TIMEOUT"})
    kwargs = mock_create.await_args.kwargs
    assert kwargs["body"] == 'Run for "[unknown]" failed with error: E_TIMEOUT.'


async def test_missing_pipeline_name_key_does_not_crash(mapper: NotificationEventMapper) -> None:
    """A payload missing a template key must still produce a notification body."""
    _, mock_create = await _call(mapper, "hitl_overdue", payload={"minutes_overdue": 12})
    kwargs = mock_create.await_args.kwargs
    assert kwargs["body"] == 'Pipeline "[unknown]" has been awaiting human review for 12 minutes.'


# ---------------------------------------------------------------------------
# Expiry computation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "ttl_hours"),
    [("hitl_awaiting", 72), ("run_failed", 168), ("claim_expired", 24), ("eval_regression", 336)],
)
async def test_expires_at_computed_from_ttl(mapper: NotificationEventMapper, event_type: str, ttl_hours: int) -> None:
    before = datetime.now(UTC)
    _, mock_create = await _call(mapper, event_type)
    kwargs = mock_create.await_args.kwargs
    expires_at = kwargs["expires_at"]
    assert expires_at is not None
    delta = expires_at - before
    assert delta.total_seconds() == pytest.approx(ttl_hours * 3600, abs=30)


async def test_system_announcement_has_no_expiry(mapper: NotificationEventMapper) -> None:
    """system_announcement configures ttl_hours=None, so no expires_at is passed."""
    _, mock_create = await _call(mapper, "system_announcement")
    kwargs = mock_create.await_args.kwargs
    assert kwargs["expires_at"] is None


# ---------------------------------------------------------------------------
# target_user_id passthrough
# ---------------------------------------------------------------------------


async def test_target_user_id_passed_through(mapper: NotificationEventMapper) -> None:
    result, mock_create = await _call(
        mapper,
        "feedback_pending",
        target_user_id=_TARGET_USER,
    )
    assert result is not None
    kwargs = mock_create.await_args.kwargs
    assert kwargs["target_user_id"] == _TARGET_USER


# ---------------------------------------------------------------------------
# _resolve_template unit tests
# ---------------------------------------------------------------------------


def test_resolve_template_substitutes_all_keys(mapper: NotificationEventMapper) -> None:
    assert mapper._resolve_template("hi {name}!", {"name": "bob"}) == "hi bob!"


def test_resolve_template_missing_key_replaced(mapper: NotificationEventMapper) -> None:
    assert mapper._resolve_template("hi {name}!", {}) == "hi [unknown]!"


def test_resolve_template_second_missing_key_keeps_placeholder(mapper: NotificationEventMapper) -> None:
    result = mapper._resolve_template("{a} {b} {c}", {"a": "x"})
    assert result == "{a} [unknown] {c}"


def test_resolve_template_valueerror_returns_template(mapper: NotificationEventMapper) -> None:
    assert mapper._resolve_template("{a:03d}", {"a": "not-a-number"}) == "{a:03d}"
