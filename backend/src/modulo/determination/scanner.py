"""DeterminationScanner — samples connector data for SDLC assessment.

Read-only. Never writes to any external system.
Each sample is a dict with connector_type, resource, and raw records.
"""

import uuid

from modulo.connectors.base import ConnectorBase, ConnectorQuery, ConnectorType
from modulo.core.connector_hub import ConnectorHub


class ScanSample:
    """A single sample of data from a connector."""

    def __init__(
        self,
        connector_id: uuid.UUID,
        connector_type: ConnectorType,
        resource: str,
        records: list[dict],
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


def _repo_name(rec: dict) -> str:
    """Extract a repo/project identifier from a record.

    Handles GitHub (full_name or name) and GitLab (path_with_namespace or name) formats.
    """
    return rec.get("full_name") or rec.get("path_with_namespace") or rec.get("name", "")


def _add(
    samples: list[ScanSample],
    connector_id: uuid.UUID,
    ct: ConnectorType,
    resource: str,
    records: list[dict],
    sample_count: int,
    error: str | None = None,
) -> None:
    samples.append(ScanSample(connector_id, ct, resource, records, sample_count, error))


async def _sample_connector(connector_id: uuid.UUID, connector: ConnectorBase) -> list[ScanSample]:
    """Sample data from a single connector based on its type."""
    samples: list[ScanSample] = []

    await connector.health_check()
    ct = connector.connector_type

    match ct:
        case ConnectorType.FILESYSTEM:
            return samples

        case ConnectorType.GITHUB:
            repos: list[dict] = []
            try:
                r = await connector.query(ConnectorQuery(resource="repos", limit=_SAMPLE_LIMIT))
                repos = r.records
                _add(samples, connector_id, ct, "repos", repos, len(repos))
            except Exception as exc:
                _add(samples, connector_id, ct, "repos", [], 0, str(exc)[:200])

            if repos:
                first = _repo_name(repos[0])
                try:
                    r = await connector.query(
                        ConnectorQuery(resource="pulls", filters={"repo": first, "state": "open"})
                    )
                    _add(samples, connector_id, ct, "pulls", r.records, len(r.records))
                except Exception as exc:
                    _add(samples, connector_id, ct, "pulls", [], 0, str(exc)[:200])

        case ConnectorType.GITLAB:
            projects: list[dict] = []
            try:
                r = await connector.query(ConnectorQuery(resource="projects", limit=_SAMPLE_LIMIT))
                projects = r.records
                _add(samples, connector_id, ct, "projects", projects, len(projects))
            except Exception as exc:
                _add(samples, connector_id, ct, "projects", [], 0, str(exc)[:200])

            if projects:
                first = _repo_name(projects[0])
                try:
                    r = await connector.query(
                        ConnectorQuery(resource="mrs", filters={"project": first, "state": "opened"})
                    )
                    _add(samples, connector_id, ct, "mrs", r.records, len(r.records))
                except Exception as exc:
                    _add(samples, connector_id, ct, "mrs", [], 0, str(exc)[:200])

        case ConnectorType.JIRA:
            try:
                r = await connector.query(
                    ConnectorQuery(
                        resource="search",
                        filters={"jql": "ORDER BY created DESC", "max_results": _SAMPLE_LIMIT},
                    )
                )
                _add(samples, connector_id, ct, "issues", r.records, r.total or len(r.records))
            except Exception as exc:
                _add(samples, connector_id, ct, "issues", [], 0, str(exc)[:200])

        case ConnectorType.LINEAR:
            try:
                r = await connector.query(ConnectorQuery(resource="search", filters={"query": ""}))
                _add(samples, connector_id, ct, "issues", r.records[:_SAMPLE_LIMIT], len(r.records))
            except Exception as exc:
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
            continue
        samples = await _sample_connector(connector_id, connector)
        all_samples.extend(samples)
    return all_samples
