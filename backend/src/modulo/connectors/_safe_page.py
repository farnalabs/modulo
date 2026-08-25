"""Shared safe-page extraction helper for connector clients.

The Azure Repos (``value``), Azure Pipelines (``value``), Azure Key Vault
(``value``), Bitbucket (``values``), Microsoft Teams (``value``), SharePoint
(``value``), TeamCity (``build``/``project``/``buildType``/``agent``),
CircleCI (``items``), GitHub Actions (``workflow_runs``), n8n (``data``),
Opsgenie (``data``), Datadog (``data``), Asana (``data``), Snyk (``data``),
SonarQube (``components``/``analyses``/``issues``/``qualitygates``/
``metrics``/``plugins``/``hotspots``), PagerDuty
(``incidents``/``services``/``teams``/``users``/``escalation_policies``/
``schedules``/``oncalls``), Slack (``channels``/``messages``/``members``/
``scheduled_messages``), npm (``objects``), Notion (``results``),
Confluence (``results``), CodeClimate (``data``),
Jenkins (``builds``/``jobs``/``computer``), and Dropbox Paper
(``doc_ids``/``entries``) connectors each guard their list parsing against
corrupt or hostile response bodies. A corrupt or hostile response may return
a non-dict body (list, string, number, ...) or a non-list page field — either
crashes the connector with ``AttributeError`` on the bare
``body.get(key, [])`` chain or returns a bare string as the records list.
The Grafana connector (a bare top-level array body) is guarded by
``safe_records_list``. ``safe_paging_total`` centralises the per-connector
``_paging_total`` extraction (Azure Pipelines/Repos ``count``, Bitbucket
``size``, Opsgenie ``totalCount``, PagerDuty ``total``, SonarQube
``paging.total``). Keeping a single implementation in one place avoids drift
between the copies (mirrors ``_safe_int`` / ``_safe_cursor`` /
``_safe_datetime``).
"""

from __future__ import annotations

from typing import Any

from modulo.connectors._safe_int import safe_int as _safe_int


def safe_records(body: object, key: str) -> list[dict[str, Any]]:
    """Return the *key* page list from *body*, or an empty page for corrupt bodies.

    Only a dict body whose *key* field holds a list yields records; anything
    else (non-dict body, missing key, non-list value) falls back to an empty
    page so the caller's list query degrades gracefully instead of crashing.
    """
    if not isinstance(body, dict):
        return []
    records = body.get(key, [])
    return records if isinstance(records, list) else []


def safe_records_list(body: object) -> list[dict[str, Any]]:
    """Return a bare top-level array body, or an empty page for a corrupt body.

    Some APIs (Grafana) return the records as the top-level JSON array rather
    than nest them under a key. Only a list body yields records; a non-list
    body (dict, string, number, ...) falls back to an empty page so the
    caller's list query degrades gracefully instead of crashing on a bare
    ``body[key]`` or ``body[: limit]`` slice against a non-sequence.
    """
    if not isinstance(body, list):
        return []
    return [rec for rec in body if isinstance(rec, dict)]


def safe_paging_total(body: object, *keys: str) -> int | None:
    """Extract a paging total field from *body* as a safe int.

    Walks ``*keys`` through nested dict bodies (``safe_paging_total(body,
    "paging", "total")``), returning ``None`` when a hop is missing or is not
    a dict. The final value is coerced via ``safe_int``; non-finite floats
    (``inf``/``nan``) are rejected so a corrupt or hostile response cannot
    poison the reported total or downstream aggregation. The Azure DevOps
    (``count``), Bitbucket (``size``), Opsgenie (``totalCount``), PagerDuty
    (``total``), and SonarQube (``paging.total``) connectors each guarded this
    extraction with a private ``_paging_total`` copy; keeping a single
    implementation in one place avoids drift (mirrors ``_safe_int`` /
    ``_safe_cursor`` / ``_safe_records``).
    """
    if not isinstance(body, dict):
        return None
    node: object = body
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return _safe_int(node)
