"""Shared builders for the reports unit test package.

Consolidates the async-session double, the session factory, and the
report mock that ``test_report_scheduler.py`` and
``test_cost_report_scheduler.py`` each re-implemented with slightly
different shapes. Changes to how the scheduler / CRUD layer interacts with
``AsyncSession`` now only have to be made once.
"""

from __future__ import annotations

import uuid
from typing import Self
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
