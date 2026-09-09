"""DeterminationScanner — samples connector data for SDLC assessment.

Read-only. Never writes to any external system.
Each sample is a dict with connector_type, resource, and raw records.
"""

import asyncio
import logging
import uuid
from typing import Any

from modulo.connectors.base import ConnectorBase, ConnectorQuery, ConnectorType
from modulo.core.connector_hub import ConnectorHub

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT = 30.0


class ScanSample:
    """A single sample of data from a connector."""

    def __init__(
        self,
        connector_id: uuid.UUID,
        connector_type: ConnectorType,
        resource: str,
        records: list[dict[str, Any]],
        sample_count: int,
        error: str | None = None,
    ) -> None:
        self.connector_id = connector_id
        self.connector_type = connector_type
        self.resource = resource
        self.records = records
        self.sample_count = sample_count
        self.error = error


_SAMPLE_LIMIT = 25


def _repo_name(rec: dict[str, Any]) -> str:
    """Extract a repo/project identifier from a record.

    Handles GitHub (full_name or name) and GitLab (path_with_namespace or name) formats.
    """
    for key in ("full_name", "path_with_namespace", "name"):
        value = rec.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _add(
    samples: list[ScanSample],
    connector_id: uuid.UUID,
    ct: ConnectorType,
    resource: str,
    records: list[dict[str, Any]],
    sample_count: int,
    error: str | None = None,
) -> None:
    samples.append(ScanSample(connector_id, ct, resource, records, sample_count, error))


async def _query_with_timeout(connector: ConnectorBase, query: ConnectorQuery) -> Any:
    try:
        return await asyncio.wait_for(connector.query(query), timeout=_QUERY_TIMEOUT)
    except TimeoutError:
        raise TimeoutError(f"connector query '{query.resource}' timed out after {_QUERY_TIMEOUT}s") from None


async def _sample_query(
    connector: ConnectorBase,
    connector_id: uuid.UUID,
    resource: str,
    query: ConnectorQuery,
) -> tuple[Any | None, str | None]:
    """Run a sampling query, converting failures into ``(result, error)``.

    Returns ``(result, None)`` on success and ``(None, error)`` on failure so a
    single connector's query error becomes an error sample instead of aborting
    the whole scan. ``CancelledError`` is always re-raised.
    """
    try:
        return await _query_with_timeout(connector, query), None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Sampling %s failed for connector %s: %s", resource, connector_id, exc)
        return None, str(exc)[:200]


async def _sample_repo_family(
    connector: ConnectorBase,
    connector_id: uuid.UUID,
    ct: ConnectorType,
    *,
    list_resource: str,
    detail_resource: str,
    detail_filter_key: str,
    open_state: str,
) -> list[ScanSample]:
    """Sample the top-level listing (repos/projects), then the open PRs/MRs of each named entry.

    Shared shape of the GITHUB (repos -> pulls) and GITLAB (projects -> mrs)
    sampling flows: a bounded listing query first, then one detail query per
    entry that has a usable name.
    """
    samples: list[ScanSample] = []
    listing_result, listing_error = await _sample_query(
        connector, connector_id, list_resource, ConnectorQuery(resource=list_resource, limit=_SAMPLE_LIMIT)
    )
    listing = listing_result.records if listing_result is not None else []
    _add(samples, connector_id, ct, list_resource, listing, len(listing), listing_error)

    for item in listing:
        name = _repo_name(item)
        if not name:
            continue
        detail_result, detail_error = await _sample_query(
            connector,
            connector_id,
            detail_resource,
            ConnectorQuery(resource=detail_resource, filters={detail_filter_key: name, "state": open_state}),
        )
        details = detail_result.records if detail_result is not None else []
        _add(samples, connector_id, ct, detail_resource, details, len(details), detail_error)
    return samples


async def _sample_jira_issues(connector: ConnectorBase, connector_id: uuid.UUID, ct: ConnectorType) -> list[ScanSample]:
    """Sample Jira issues (most recent first, bounded by the sample limit)."""
    samples: list[ScanSample] = []
    result, error = await _sample_query(
        connector,
        connector_id,
        "issues",
        ConnectorQuery(resource="search", filters={"jql": "ORDER BY created DESC", "max_results": _SAMPLE_LIMIT}),
    )
    _add(
        samples,
        connector_id,
        ct,
        "issues",
        result.records if result is not None else [],
        (getattr(result, "total", None) or len(result.records)) if result is not None else 0,
        error,
    )
    return samples


async def _sample_connector(connector_id: uuid.UUID, connector: ConnectorBase) -> list[ScanSample]:
    """Sample data from a single connector based on its type."""
    samples: list[ScanSample] = []

    await connector.health_check()
    ct = connector.connector_type

    if ct == ConnectorType.GITHUB:
        samples.extend(
            await _sample_repo_family(
                connector,
                connector_id,
                ct,
                list_resource="repos",
                detail_resource="pulls",
                detail_filter_key="repo",
                open_state="open",
            )
        )
    elif ct == ConnectorType.GITLAB:
        samples.extend(
            await _sample_repo_family(
                connector,
                connector_id,
                ct,
                list_resource="projects",
                detail_resource="mrs",
                detail_filter_key="project",
                open_state="opened",
            )
        )
    elif ct == ConnectorType.JIRA:
        samples.extend(await _sample_jira_issues(connector, connector_id, ct))

    return samples


async def run_scan(hub: ConnectorHub) -> list[ScanSample]:
    """Sample data from every registered connector.

    Returns a flat list of ScanSample objects, one per sampled resource per connector.
    Connectors that fail health_check produce zero samples.
    """
    all_samples: list[ScanSample] = []
    for connector_id in hub.connector_ids:
        try:
            connector = hub.get(connector_id)
        except KeyError:
            logger.warning("Connector %s not found in hub during scan", connector_id)
            continue
        except Exception as exc:
            logger.warning("Connector %s retrieval failed: %s", connector_id, exc)
            continue
        try:
            samples = await _sample_connector(connector_id, connector)
            all_samples.extend(samples)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Connector %s sampling failed", connector_id)
    return all_samples
