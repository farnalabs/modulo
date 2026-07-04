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

from modulo.db.crud.notifications import create_notification
from modulo.db.models.notification import Notification

_log = logging.getLogger(__name__)

_EVENT_CONFIG: dict[str, dict[str, Any]] = {
    "hitl_awaiting": {
        "level": "info",
        "scope": "org",
        "category": "hitl.awaiting",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 72,
    },
    "run_failed": {
        "level": "error",
        "scope": "org",
        "category": "run.failed",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    "budget_exceeded": {
        "level": "warning",
        "scope": "org",
        "category": "run.budget_exceeded",
        "dismiss_strategy": "org_admin",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    "claim_expired": {
        "level": "info",
        "scope": "org",
        "category": "hitl.claim_expired",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 24,
    },
    "hitl_overdue": {
        "level": "warning",
        "scope": "admin",
        "category": "hitl.overdue",
        "dismiss_strategy": "org_admin",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    "eval_regression": {
        "level": "warning",
        "scope": "org",
        "category": "eval.regression",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 336,
    },
    "eval_blocked": {
        "level": "error",
        "scope": "org",
        "category": "eval.blocked",
        "dismiss_strategy": "any_scope",
        "dismissible_at_scope": True,
        "ttl_hours": 168,
    },
    "feedback_pending": {
        "level": "info",
        "scope": "user",
        "category": "feedback.pending",
        "dismiss_strategy": "user_only",
        "dismissible_at_scope": False,
        "ttl_hours": 336,
    },
    "system_announcement": {
        "level": "info",
        "scope": "org",
        "category": "system.announcement",
        "dismiss_strategy": "org_admin",
        "dismissible_at_scope": True,
        "ttl_hours": None,
    },
}

_TITLE_TEMPLATES: dict[str, str] = {
    "hitl_awaiting": "HITL review needed — {pipeline_name}",
    "run_failed": "Run failed — {pipeline_name}",
    "budget_exceeded": "Budget exceeded — {pipeline_name}",
    "claim_expired": "HITL claim expired — {pipeline_name}",
    "hitl_overdue": "HITL overdue — {pipeline_name}",
    "eval_regression": "Eval regression detected — {agent_name}",
    "eval_blocked": "Eval blocked — {pipeline_name}",
    "feedback_pending": "Feedback awaiting review",
    "system_announcement": "System announcement",
}

_BODY_TEMPLATES: dict[str, str] = {
    "hitl_awaiting": "Pipeline \"{pipeline_name}\" is waiting for human review.",
    "run_failed": "Run for \"{pipeline_name}\" failed with error: {error_code}.",
    "budget_exceeded": "Run for \"{pipeline_name}\" exceeded its token budget.",
    "claim_expired": "A HITL claim on \"{pipeline_name}\" has expired.",
    "hitl_overdue": "Pipeline \"{pipeline_name}\" has been awaiting human review for {minutes_overdue} minutes.",
    "eval_regression": "Eval pass rate dropped for agent \"{agent_name}\".",
    "eval_blocked": "An eval check blocked pipeline \"{pipeline_name}\".",
    "feedback_pending": "A feedback record is pending your review.",
    "system_announcement": "{message}",
}

_ACTION_URL_TEMPLATES: dict[str, str] = {
    "hitl_awaiting": "/runs/{run_id}",
    "run_failed": "/runs/{run_id}",
    "budget_exceeded": "/runs/{run_id}",
    "claim_expired": "/runs/{run_id}",
    "hitl_overdue": "/runs/{run_id}",
    "eval_regression": "/evals",
    "eval_blocked": "/runs/{run_id}",
    "feedback_pending": "/feedback/inbox",
    "system_announcement": None,
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
            return template.replace(f"{{{exc.args[0]}}}", "[unknown]", 1)
        except (ValueError, IndexError, TypeError):
            _log.warning("mapper.template_format_error", extra={"template": template})
            return template
