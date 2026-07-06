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
    return rec.get("full_name") or rec.get("path_with_namespace") or rec.get("name", "")


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


async def _query_with_timeout(
    connector: ConnectorBase, query: ConnectorQuery
) -> Any:
    return await asyncio.wait_for(
        connector.query(query), timeout=_QUERY_TIMEOUT
    )


async def _sample_connector(connector_id: uuid.UUID, connector: ConnectorBase) -> list[ScanSample]:
    """Sample data from a single connector based on its type."""
    samples: list[ScanSample] = []

    await connector.health_check()
    ct = connector.connector_type

    match ct:
        case ConnectorType.FILESYSTEM:
            return samples

        case ConnectorType.GITHUB:
            repos: list[dict[str, Any]] = []
            try:
                r = await _query_with_timeout(
                    connector, ConnectorQuery(resource="repos", limit=_SAMPLE_LIMIT)
                )
                repos = r.records
                _add(samples, connector_id, ct, "repos", repos, len(repos))
            except Exception as exc:
                logger.warning("GitHub repo sampling failed for connector %s: %s", connector_id, exc)
                _add(samples, connector_id, ct, "repos", [], 0, str(exc)[:200])

            for repo in repos:
                name = _repo_name(repo)
                if not name:
                    continue
                try:
                    r = await _query_with_timeout(
                        connector,
                        ConnectorQuery(resource="pulls", filters={"repo": name, "state": "open"}),
                    )
                    _add(samples, connector_id, ct, "pulls", r.records, len(r.records))
                except Exception as exc:
                    logger.warning(
                        "GitHub PR sampling failed for connector %s repo %s: %s",
                        connector_id,
                        name,
                        exc,
                    )
                    _add(samples, connector_id, ct, "pulls", [], 0, str(exc)[:200])

        case ConnectorType.GITLAB:
            projects: list[dict[str, Any]] = []
            try:
                r = await _query_with_timeout(
                    connector, ConnectorQuery(resource="projects", limit=_SAMPLE_LIMIT)
                )
                projects = r.records
                _add(samples, connector_id, ct, "projects", projects, len(projects))
            except Exception as exc:
                logger.warning("GitLab project sampling failed for connector %s: %s", connector_id, exc)
                _add(samples, connector_id, ct, "projects", [], 0, str(exc)[:200])

            for project in projects:
                name = _repo_name(project)
                if not name:
                    continue
                try:
                    r = await _query_with_timeout(
                        connector,
                        ConnectorQuery(resource="mrs", filters={"project": name, "state": "opened"}),
                    )
                    _add(samples, connector_id, ct, "mrs", r.records, len(r.records))
                except Exception as exc:
                    logger.warning(
                        "GitLab MR sampling failed for connector %s project %s: %s",
                        connector_id,
                        name,
                        exc,
                    )
                    _add(samples, connector_id, ct, "mrs", [], 0, str(exc)[:200])

        case ConnectorType.JIRA:
            try:
                r = await _query_with_timeout(
                    connector,
                    ConnectorQuery(
                        resource="search",
                        filters={"jql": "ORDER BY created DESC", "max_results": _SAMPLE_LIMIT},
                    ),
                )
                _add(samples, connector_id, ct, "issues", r.records, r.total or len(r.records))
            except Exception as exc:
                logger.warning("JIRA sampling failed for connector %s: %s", connector_id, exc)
                _add(samples, connector_id, ct, "issues", [], 0, str(exc)[:200])

        case ConnectorType.LINEAR:
            try:
                r = await _query_with_timeout(
                    connector, ConnectorQuery(resource="search", filters={"query": ""})
                )
                _add(
                    samples,
                    connector_id,
                    ct,
                    "issues",
                    r.records[:_SAMPLE_LIMIT],
                    min(len(r.records), _SAMPLE_LIMIT),
                )
            except Exception as exc:
                logger.warning("Linear sampling failed for connector %s: %s", connector_id, exc)
                _add(samples, connector_id, ct, "issues", [], 0, str(exc)[:200])

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
        samples = await _sample_connector(connector_id, connector)
        all_samples.extend(samples)
    return all_samples
