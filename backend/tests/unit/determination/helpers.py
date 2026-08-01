"""Shared factories for the determination unit test package."""

import uuid

from modulo.connectors.base import ConnectorType
from modulo.determination.inference import Finding
from modulo.determination.scanner import ScanSample


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


def make_finding(category: str, finding: str, confidence: str = "high") -> Finding:
    return Finding(category=category, finding=finding, evidence="test", confidence=confidence)
