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
from contextlib import contextmanager
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.sql.elements import BinaryExpression

from modulo.db.models.scheduled_report import ScheduledReport

# Shared Slack webhook URLs for the reports tests — a single definition so the
# quality and scheduler suites cannot drift apart.
SLACK_URL = "https://hooks.slack.com/services/T1/B1/xxx"
SLACK_URL_2 = "https://hooks.slack.com/services/T1/B2/yyy"

# Sentinel distinguishing "no value expected" from an expected ``None``.
_UNSET = object()


class MockBegin:
    """Minimal async context manager standing in for ``Session.begin()``."""

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

    def begin(self) -> MockBegin:
        return MockBegin()

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
    return make_report_mock(
        report_type="cost",
        cron_expression="0 0 * * *",
        config_json={
            "period": "daily",
            "group_by": "team",
            "format": "csv",
            "schedule_type": schedule_type,
        },
        recipient_config={"type": "email", "emails": ["admin@example.com"]},
    )


def make_http_client(side_effect: list[object] | None = None) -> MagicMock:
    """Build an async HTTP client double whose ``post`` replays *side_effect*."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=side_effect or [])
    return client


def make_http_response(
    *,
    is_success: bool | None = None,
    status_code: int = 200,
    text: str = "ok",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a minimal ``httpx.Response``-shaped double.

    ``is_success`` defaults to being derived from ``status_code`` so a
    non-2xx response is consistent with its status unless a caller explicitly
    overrides it.
    """
    resp = MagicMock()
    resp.is_success = status_code < 400 if is_success is None else is_success
    resp.status_code = status_code
    resp.text = text
    resp.headers = {} if headers is None else headers
    return resp


def binary_expressions(clause: Any) -> Iterator[Any]:
    """Yield every ``BinaryExpression`` nested inside a SQL clause.

    Lets tests assert on SQL predicate structure (operator + column) without
    matching on rendered SQL text. A single-predicate ``.where()`` collapses to
    the ``BinaryExpression`` itself, so that case is yielded directly rather
    than being lost when iterating children.
    """
    if clause is None:
        return
    if isinstance(clause, BinaryExpression):
        yield clause
    for child in clause.get_children():
        yield from binary_expressions(child)


def has_predicate(clause: Any, operator: Any, column_name: str, value: Any = _UNSET) -> bool:
    """True when *clause* contains a BinaryExpression with the given operator and
    left column name (and, when *value* is provided, that right-side value)."""
    for pred in binary_expressions(clause):
        if pred.operator is not operator:
            continue
        if getattr(pred.left, "name", None) != column_name:
            continue
        if value is not _UNSET and getattr(pred.right, "value", _UNSET) != value:
            continue
        return True
    return False


@contextmanager
def report_firing_env(session: MockSession) -> Iterator[None]:
    """Patch the scheduler's engine/session/RLS plumbing to run *session*.

    Collapses the three-patch prologue every ``_fire_scheduled_report`` test
    repeats: the engine singleton is stubbed out, the async session factory
    hands out *session*, and RLS-org setup is a no-op. Tests that also stub the
    registry or ``compute_next_send`` stack those patches on top in the same
    ``with`` block.
    """
    with (
        patch("modulo.core.reports.scheduler._get_engine"),
        patch(
            "modulo.core.reports.scheduler.async_sessionmaker",
            return_value=MockSessionFactory(session),
        ),
        patch("modulo.core.reports.scheduler._set_rls_org", new_callable=AsyncMock),
    ):
        yield


@contextmanager
def patched_http_client(client: AsyncMock) -> Iterator[MagicMock]:
    """Make the scheduler's ``httpx.AsyncClient(...)`` hand out *client*.

    Yields the mocked client class so tests that must inspect the constructor
    call (e.g. the timeout pass-through contract) can assert on its kwargs.
    Both ``scheduler`` and ``quality_report`` call the same ``httpx`` module
    attribute, so one patch covers delivery through either.
    """
    with patch("modulo.core.reports.scheduler.httpx.AsyncClient") as client_cls:
        client_cls.return_value.__aenter__.return_value = client
        yield client_cls


@contextmanager
def patched_quality_delivery(client: AsyncMock) -> Iterator[MagicMock]:
    """Delivery env for ``deliver_quality_report`` tests: retry budget 1 (no
    retry delays), retry sleeps stubbed, and ``httpx.AsyncClient`` hands out
    *client*. Yields the mocked client class for constructor assertions."""
    with (
        patch("modulo.core.reports.scheduler._REPORT_MAX_RETRIES", 1),
        patch("modulo.core.reports.scheduler.asyncio.sleep", new_callable=AsyncMock),
        patched_http_client(client) as client_cls,
    ):
        yield client_cls
