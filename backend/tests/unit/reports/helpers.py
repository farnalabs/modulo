"""Shared builders for the reports unit test package.

Consolidates the async-session double, the session factory, and the
report mock that ``test_report_scheduler.py`` and
``test_cost_report_scheduler.py`` each re-implemented with slightly
different shapes. Changes to how the scheduler / CRUD layer interacts with
``AsyncSession`` now only have to be made once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

from modulo.db.models.scheduled_report import ScheduledReport


class _MockBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class MockSession:
    """AsyncSession double whose ``execute()`` returns queued results.

    ``execute_side_effect`` is consumed in order per ``execute()`` call —
    matching the read-then-write flow of ``_fire_scheduled_report`` (SELECT,
    then UPDATE). Tracks ``added`` objects for ``add()`` and exposes
    ``delete``/``flush`` for the CRUD paths.
    """

    def __init__(self, execute_side_effect: list[MagicMock] | None = None) -> None:
        self.execute = AsyncMock(side_effect=execute_side_effect or [])
        self.delete = AsyncMock()
        self.flush = AsyncMock()
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _MockBegin:
        return _MockBegin()

    def add(self, obj: object) -> None:
        self.added.append(obj)


class MockSessionFactory:
    def __init__(self, session: MockSession) -> None:
        self._session = session

    def __call__(self) -> MockSession:
        return self._session


def make_report_mock(
    *,
    active: bool = True,
    report_type: str = "quality",
    cron_expression: str = "0 9 * * 1",
    config_json: dict | None = None,
    recipient_config: dict | None = None,
) -> MagicMock:
    """Build a MagicMock exposing the ScheduledReport surface under test."""
    report = MagicMock(spec=ScheduledReport)
    report.id = uuid.uuid4()
    report.organisation_id = uuid.uuid4()
    report.active = active
    report.report_type = report_type
    report.cron_expression = cron_expression
    report.config_json = config_json or {}
    report.recipient_config = recipient_config or {}
    return report


def make_cost_report_mock(*, schedule_type: str) -> MagicMock:
    """Build the cost-report ScheduledReport double shared by the cost tests."""
    report = MagicMock(spec=ScheduledReport)
    report.id = uuid.uuid4()
    report.organisation_id = uuid.uuid4()
    report.active = True
    report.report_type = "cost"
    report.cron_expression = "0 0 * * *"
    report.config_json = {
        "period": "daily",
        "group_by": "team",
        "format": "csv",
        "schedule_type": schedule_type,
    }
    report.recipient_config = {"type": "email", "emails": ["admin@example.com"]}
    return report


def make_http_client(side_effect: list[object] | None = None) -> MagicMock:
    """Build an async HTTP client double whose ``post`` replays *side_effect*."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=side_effect or [])
    return client


def make_http_response(
    *,
    is_success: bool = True,
    status_code: int = 200,
    text: str = "ok",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a minimal ``httpx.Response``-shaped double."""
    resp = MagicMock()
    resp.is_success = is_success
    resp.status_code = status_code
    resp.text = text
    resp.headers = {} if headers is None else headers
    return resp


def binary_expressions(clause: Any) -> Iterator[Any]:
    """Yield every ``BinaryExpression`` nested inside a SQL clause.

    Lets tests assert on SQL predicate structure (operator + column) without
    matching on rendered SQL text.
    """
    from sqlalchemy.sql.elements import BinaryExpression

    for child in clause.get_children():
        if isinstance(child, BinaryExpression):
            yield child
        yield from binary_expressions(child)
