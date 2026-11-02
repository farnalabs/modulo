"""Shared factories for the determination unit test package."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from modulo.connectors.base import ConnectorQuery, ConnectorType
from modulo.determination.draft import PipelineDraft, generate_draft
from modulo.determination.inference import Finding, infer
from modulo.determination.scanner import ScanSample


def iso_days_ago(delta_days: float) -> str:
    """Return an ISO-8601 timestamp that many days in the past (deterministic across run dates)."""
    return (datetime.now(UTC) - timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_sample(
    resource: str,
    records: list[dict],
    connector_type: ConnectorType = ConnectorType.GITHUB,
    error: str | None = None,
) -> ScanSample:
    return ScanSample(
        connector_id=uuid.uuid4(),
        connector_type=connector_type,
        resource=resource,
        records=records,
        sample_count=len(records),
        error=error,
    )


def issues_sample(statuses: list[str]) -> ScanSample:
    """Return a Jira ``issues`` sample whose records expose *statuses* via ``fields.status``.

    Every planning/transition test needs the same Jira-shaped record; building it
    here keeps each test down to the statuses it actually cares about.
    """
    return make_sample(
        "issues",
        [{"fields": {"status": {"name": status}}} for status in statuses],
        connector_type=ConnectorType.JIRA,
    )


class FakeConnector:
    """Stub connector for scanner tests, with each behaviour injected by the caller.

    ``health_check_error`` / ``query_error`` are raised from the corresponding
    method (use for skip and cancellation cases), ``query_records`` is returned
    wrapped in a query result, and a stub with neither set fails loudly on
    ``query`` — most stubs must never be queried at all.
    """

    def __init__(
        self,
        connector_type: ConnectorType = ConnectorType.GITHUB,
        *,
        health_check_error: BaseException | None = None,
        query_error: BaseException | None = None,
        query_records: list[dict] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.connector_type = connector_type
        self._health_check_error = health_check_error
        self._query_error = query_error
        self._query_records = query_records
        self._delay_seconds = delay_seconds

    async def health_check(self) -> None:
        if self._health_check_error is not None:
            raise self._health_check_error

    async def query(self, _query: ConnectorQuery) -> Any:
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._query_error is not None:
            raise self._query_error
        if self._query_records is None:
            raise AssertionError("query should not be reached for this stub connector")
        return SimpleNamespace(records=list(self._query_records))


def make_finding(category: str, finding: str, confidence: str = "high") -> Finding:
    return Finding(category=category, finding=finding, evidence="test", confidence=confidence)


def draft_for(samples: list[ScanSample]) -> PipelineDraft:
    """Run the inference engine over *samples* and build the draft pipeline from it.

    Every draft test needs the same infer-then-draft pair; doing it here keeps
    each test focused on the draft behaviour it is actually asserting.
    """
    return generate_draft(samples, infer(samples))
