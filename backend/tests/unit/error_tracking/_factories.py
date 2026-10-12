"""Shared factories for error-tracking unit tests (FAR-1676).

One canonical definition of each mock/data factory so duplicated local copies
in this package's test modules can be deleted. Import with
``from tests.unit.error_tracking._factories import make_rule`` — the package
is importable (``tests.unit`` is a package) and pytest never collects this
module (name is not ``test_*`` and it defines no tests).

``make_rule`` defaults to ``signal=None`` (legacy level-matching): with
``condition_level="error"`` that is behaviourally identical to the factories
it replaces whose ``.signal`` was an incidental MagicMock attribute
(``_rule_matches`` falls back to level matching for any non-string signal).
Signal-specific tests pass ``make_rule(signal="agent.failed")`` explicitly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.error_tracking.alerting import TriggeredAlert
from modulo.db.models.error_notification_rule import ErrorNotificationRule

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
GROUP_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
EVENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
_NOW = dt.datetime.now(dt.UTC)


def make_session() -> AsyncMock:
    """Bare async-session mock."""
    return AsyncMock(spec=AsyncSession)


def make_rule(**overrides: Any) -> MagicMock:
    """ErrorNotificationRule stand-in, legacy signal semantics by default."""
    rule = MagicMock(spec=ErrorNotificationRule)
    rule.id = uuid.uuid4()
    rule.organisation_id = ORG_ID
    rule.name = "Test Rule"
    rule.enabled = True
    rule.condition_level = "error"
    rule.condition_min_count = 1
    rule.condition_window_seconds = 0
    rule.action_type = "in_app"
    rule.webhook_url = None
    rule.cooldown_seconds = 300
    rule.signal = None
    for k, v in overrides.items():
        setattr(rule, k, v)
    return rule


def session_with_rules(
    rules: list[MagicMock],
    *,
    window_counts: list[int] | None = None,
) -> AsyncMock:
    """Session whose ``execute`` returns *rules* before any window counts.

    The first ``execute`` call always returns *rules* (``evaluate()`` loads
    rules first). With ``window_counts=None`` every later ``execute`` also
    returns the rules result (the pre-window-counts tests' shape: rules with
    ``condition_window_seconds=0`` never query a window). With a list, later
    ``execute`` calls drain *window_counts* as ``scalar_one()`` results.
    """
    session = make_session()
    queue = list(window_counts or [])
    rules_result = MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rules))))
    rules_loaded = False

    def _execute(_stmt: object) -> MagicMock:
        nonlocal rules_loaded
        if not rules_loaded:
            rules_loaded = True
            return rules_result
        if queue:
            return MagicMock(scalar_one=MagicMock(return_value=queue.pop(0)))
        return rules_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def make_group(**kw: Any) -> MagicMock:
    """ErrorGroup stand-in with the dashboard surface defaults."""
    g = MagicMock()
    g.id = kw.get("id", GROUP_ID)
    g.fingerprint = kw.get("fingerprint", "fp123")
    g.status = kw.get("status", "new")
    g.level_peak = kw.get("level_peak", "error")
    g.count = kw.get("count", 5)
    g.first_seen = kw.get("first_seen", _NOW)
    g.last_seen = kw.get("last_seen", _NOW)
    g.sample_event_id = kw.get("sample_event_id", EVENT_ID)
    g.assigned_to = kw.get("assigned_to")
    return g


def make_event(**kw: Any) -> MagicMock:
    """ErrorEvent stand-in with the dashboard surface defaults."""
    e = MagicMock()
    e.id = kw.get("id", EVENT_ID)
    e.level = kw.get("level", "error")
    e.message = kw.get("message", "Something broke")
    e.stacktrace = kw.get("stacktrace", "Traceback...")
    e.context_json = kw.get("context_json", {"url": "/test"})
    e.source = kw.get("source", "backend")
    e.environment = kw.get("environment", "production")
    e.version = kw.get("version", "1.0.0")
    e.created_at = kw.get("created_at", _NOW)
    return e


def make_alert(**overrides: Any) -> TriggeredAlert:
    """TriggeredAlert stand-in with the dispatch-surface defaults."""
    fields: dict[str, Any] = {
        "rule_id": uuid.uuid4(),
        "rule_name": "Test Rule",
        "action_type": "webhook",
        "webhook_url": "https://example.com/hook",
        "error_group_id": GROUP_ID,
        "fingerprint": "fp123",
        "level": "error",
        "count": 3,
        "environment": "staging",
    }
    fields.update(overrides)
    return TriggeredAlert(**fields)


def make_membership(account_id: uuid.UUID) -> MagicMock:
    m = MagicMock()
    m.account_id = account_id
    return m
