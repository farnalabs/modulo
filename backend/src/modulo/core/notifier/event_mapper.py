"""Notification event mapping — maps platform events to in-app Notification records.

Event categories and their notification config:
  - hitl_awaiting     → level: info,   scope: org,   category: "hitl.awaiting"
  - run_failed        → level: error,  scope: org,   category: "run.failed"
  - budget_exceeded   → level: warning, scope: org,   category: "run.budget_exceeded"
  - claim_expired     → level: info,   scope: org,   category: "hitl.claim_expired"
  - hitl_overdue      → level: warning, scope: admin
  - eval_regression   → level: warning, scope: org
  - feedback_pending  → level: info,   scope: user (target_user_id assigned)
  - system_announcement → level: info,  scope: org
  - eval_blocked      → level: error,  scope: org
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.notifier import (
    EVENT_BUDGET_EXCEEDED,
    EVENT_CLAIM_EXPIRED,
    EVENT_EVAL_BLOCKED,
    EVENT_EVAL_REGRESSION,
    EVENT_FEEDBACK_PENDING,
    EVENT_HITL_AWAITING,
    EVENT_HITL_OVERDUE,
    EVENT_RUN_FAILED,
    EVENT_SYSTEM_ANNOUNCEMENT,
)
from modulo.db.crud.notifications import create_notification
from modulo.db.models.notification import Notification

_log = logging.getLogger(__name__)

_EVENT_CONFIG: dict[str, dict[str, Any]] = {
    EVENT_HITL_AWAITING: {
        "level": "info",
        "scope": "org",
        "category": "hitl.awaiting",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 72,
    },
    EVENT_RUN_FAILED: {
        "level": "error",
        "scope": "org",
        "category": "run.failed",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    EVENT_BUDGET_EXCEEDED: {
        "level": "warning",
        "scope": "org",
        "category": "run.budget_exceeded",
        "dismiss_strategy": "org_admin",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    EVENT_CLAIM_EXPIRED: {
        "level": "info",
        "scope": "org",
        "category": "hitl.claim_expired",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 24,
    },
    EVENT_HITL_OVERDUE: {
        "level": "warning",
        "scope": "admin",
        "category": "hitl.overdue",
        "dismiss_strategy": "org_admin",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    EVENT_EVAL_REGRESSION: {
        "level": "warning",
        "scope": "org",
        "category": "eval.regression",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 336,
    },
    EVENT_EVAL_BLOCKED: {
        "level": "error",
        "scope": "org",
        "category": "eval.blocked",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    EVENT_FEEDBACK_PENDING: {
        "level": "info",
        "scope": "user",
        "category": "feedback.pending",
        "dismiss_strategy": "user_only",
        "dismissible_at_scope": False,
        "ttl_hours": 336,
    },
    EVENT_SYSTEM_ANNOUNCEMENT: {
        "level": "info",
        "scope": "org",
        "category": "system.announcement",
        "dismiss_strategy": "org_admin",
        "dismissible_at_scope": True,
        "ttl_hours": None,
    },
}

_TITLE_TEMPLATES: dict[str, str] = {
    EVENT_HITL_AWAITING: "HITL review needed — {pipeline_name}",
    EVENT_RUN_FAILED: "Run failed — {pipeline_name}",
    EVENT_BUDGET_EXCEEDED: "Budget exceeded — {pipeline_name}",
    EVENT_CLAIM_EXPIRED: "HITL claim expired — {pipeline_name}",
    EVENT_HITL_OVERDUE: "HITL overdue — {pipeline_name}",
    EVENT_EVAL_REGRESSION: "Eval regression detected — {agent_name}",
    EVENT_EVAL_BLOCKED: "Eval blocked — {pipeline_name}",
    EVENT_FEEDBACK_PENDING: "Feedback awaiting review",
    EVENT_SYSTEM_ANNOUNCEMENT: "System announcement",
}

_BODY_TEMPLATES: dict[str, str] = {
    EVENT_HITL_AWAITING: "Pipeline \"{pipeline_name}\" is waiting for human review.",
    EVENT_RUN_FAILED: "Run for \"{pipeline_name}\" failed with error: {error_code}.",
    EVENT_BUDGET_EXCEEDED: "Run for \"{pipeline_name}\" exceeded its token budget.",
    EVENT_CLAIM_EXPIRED: "A HITL claim on \"{pipeline_name}\" has expired.",
    EVENT_HITL_OVERDUE: "Pipeline \"{pipeline_name}\" has been awaiting human review for {minutes_overdue} minutes.",
    EVENT_EVAL_REGRESSION: "Eval pass rate dropped for agent \"{agent_name}\".",
    EVENT_EVAL_BLOCKED: "An eval check blocked pipeline \"{pipeline_name}\".",
    EVENT_FEEDBACK_PENDING: "A feedback record is pending your review.",
    EVENT_SYSTEM_ANNOUNCEMENT: "{message}",
}

_ACTION_URL_TEMPLATES: dict[str, str | None] = {
    EVENT_HITL_AWAITING: "/runs/{run_id}",
    EVENT_RUN_FAILED: "/runs/{run_id}",
    EVENT_BUDGET_EXCEEDED: "/runs/{run_id}",
    EVENT_CLAIM_EXPIRED: "/runs/{run_id}",
    EVENT_HITL_OVERDUE: "/runs/{run_id}",
    EVENT_EVAL_REGRESSION: "/evals",
    EVENT_EVAL_BLOCKED: "/runs/{run_id}",
    EVENT_FEEDBACK_PENDING: "/feedback/inbox",
    EVENT_SYSTEM_ANNOUNCEMENT: None,
}


class NotificationEventMapper:
    """Maps platform events to in-app notifications and creates DB records."""

    async def create_from_event(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        target_user_id: uuid.UUID | None = None,
    ) -> Notification | None:
        """Create a notification record from a platform event.

        Returns None if the event type is not recognised.
        """
        config = _EVENT_CONFIG.get(event_type)
        if config is None:
            _log.debug("mapper.unknown_event_type", extra={"event_type": event_type})
            return None

        title = self._resolve_template(
            _TITLE_TEMPLATES.get(event_type, event_type),
            payload,
        )
        body = self._resolve_template(
            _BODY_TEMPLATES.get(event_type, ""),
            payload,
        )
        action_url = _ACTION_URL_TEMPLATES.get(event_type)
        if action_url is not None:
            action_url = self._resolve_template(action_url, payload)

        ttl_hours = config.get("ttl_hours")
        expires_at = None
        if ttl_hours is not None and ttl_hours > 0:
            expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)

        notification = await create_notification(
            session=session,
            org_id=org_id,
            scope=config["scope"],
            level=config["level"],
            category=config["category"],
            title=title,
            body=body,
            action_url=action_url,
            dismiss_strategy=config["dismiss_strategy"],
            dismissible_at_scope=config["dismissible_at_scope"],
            target_user_id=target_user_id,
            expires_at=expires_at,
        )
        _log.info(
            "mapper.notification_created",
            extra={
                "org_id": str(org_id),
                "event_type": event_type,
                "notification_id": str(notification.id),
            },
        )
        return notification

    def _resolve_template(self, template: str, payload: dict[str, Any]) -> str:
        try:
            return template.format(**payload)
        except KeyError as exc:
            _log.warning("mapper.template_key_missing", extra={"template": template, "key": str(exc)})
            return template.replace(f"{{{exc.args[0]}}}", "[unknown]")
        except (ValueError, IndexError, TypeError):
            _log.warning("mapper.template_format_error", extra={"template": template})
            return template
