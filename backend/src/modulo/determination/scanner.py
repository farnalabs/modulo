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


async def _sample_connector(connector_id: uuid.UUID, connector: ConnectorBase) -> list[ScanSample]:
    """Sample data from a single connector based on its type."""
    samples: list[ScanSample] = []

    await connector.health_check()
    ct = connector.connector_type

    match ct:
        case ConnectorType.FILESYSTEM:
            return samples

        case ConnectorType.GITHUB:
            repos_result, repos_error = await _sample_query(
                connector,
                connector_id,
                "repos",
                ConnectorQuery(resource="repos", limit=_SAMPLE_LIMIT),
            )
            repos = repos_result.records if repos_result is not None else []
            _add(samples, connector_id, ct, "repos", repos, len(repos), repos_error)

            for repo in repos:
                name = _repo_name(repo)
                if not name:
                    continue
                pulls_result, pulls_error = await _sample_query(
                    connector,
                    connector_id,
                    "pulls",
                    ConnectorQuery(resource="pulls", filters={"repo": name, "state": "open"}),
                )
                pulls = pulls_result.records if pulls_result is not None else []
                _add(samples, connector_id, ct, "pulls", pulls, len(pulls), pulls_error)

        case ConnectorType.GITLAB:
            projects_result, projects_error = await _sample_query(
                connector,
                connector_id,
                "projects",
                ConnectorQuery(resource="projects", limit=_SAMPLE_LIMIT),
            )
            projects = projects_result.records if projects_result is not None else []
            _add(samples, connector_id, ct, "projects", projects, len(projects), projects_error)

            for project in projects:
                name = _repo_name(project)
                if not name:
                    continue
                mrs_result, mrs_error = await _sample_query(
                    connector,
                    connector_id,
                    "mrs",
                    ConnectorQuery(resource="mrs", filters={"project": name, "state": "opened"}),
                )
                mrs = mrs_result.records if mrs_result is not None else []
                _add(samples, connector_id, ct, "mrs", mrs, len(mrs), mrs_error)

        case ConnectorType.JIRA:
            result, error = await _sample_query(
                connector,
                connector_id,
                "issues",
                ConnectorQuery(
                    resource="search",
                    filters={"jql": "ORDER BY created DESC", "max_results": _SAMPLE_LIMIT},
                ),
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

        case ConnectorType.LINEAR:
            result, error = await _sample_query(
                connector,
                connector_id,
                "issues",
                ConnectorQuery(resource="search", filters={"query": ""}),
            )
            _add(
                samples,
                connector_id,
                ct,
                "issues",
                result.records[:_SAMPLE_LIMIT] if result is not None else [],
                min(len(result.records), _SAMPLE_LIMIT) if result is not None else 0,
                error,
            )

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
