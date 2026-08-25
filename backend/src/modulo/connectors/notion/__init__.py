"""NotionConnector — async Notion REST API v1 connector."""

from typing import Any, cast

import httpx

from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.types import _DICT_STR_ANY

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class NotionConnector(ConnectorBase):
    """Read/write Notion databases, pages, blocks, and users via the REST API v1.

    Credentials (from credentials_ciphertext):
      "token" — Notion Integration Token (Bearer token)

    Supported query resources:
      "databases" — POST /search (filter value="database"); filters: {"query": "..."}
      "database"  — GET /databases/{database_id}; filters: {"database_id": "..."}
      "pages"     — POST /databases/{database_id}/query; filters: {database_id, filter, sorts}
      "page"      — GET /pages/{page_id}; filters: {"page_id": "..."}
      "blocks"    — GET /blocks/{block_id}/children; filters: {"block_id": "..."}
      "users"     — GET /users

    Supported write resources:
      "page"          — POST /pages
      "database"      — POST /databases
      "block_append"  — PATCH /blocks/{block_id}/children
      "page_update"   — PATCH /pages/{page_id}
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.NOTION

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": _NOTION_VERSION,
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_NOTION_API,
            headers=self._headers(),
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity by listing users."""
        try:
            async with self._client() as client:
                r = await client.get("/users")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            body = r.json()
            results = _safe_records(body, "results")
            return HealthResult(ok=True, detail=f"{len(results)} users accessible")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Notion API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Notion API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Notion API connection error")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "databases":
                    payload: dict[str, Any] = {
                        "filter": {"value": "database", "property": "object"},
                    }
                    if "query" in q.filters:
                        payload["query"] = q.filters["query"]
                    if q.cursor:
                        payload["start_cursor"] = q.cursor
                    if q.limit:
                        payload["page_size"] = min(q.limit, 100)
                    r = await client.post("/search", json=payload)
                    r.raise_for_status()
                    body = r.json()
                    records: list[dict[str, Any]] = _safe_records(body, "results")
                    return ConnectorResult(
                        records=records,
                        total=len(records),
                        next_cursor=_safe_cursor(body.get("next_cursor")) if isinstance(body, dict) else None,
                    )

                case "database":
                    database_id = q.filters.get("database_id")
                    if not database_id:
                        raise ValueError("Notion database query requires 'database_id' filter")
                    r = await client.get(f"/databases/{database_id}")
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(records=[body])

                case "pages":
                    database_id = q.filters.get("database_id")
                    if not database_id:
                        raise ValueError("Notion pages query requires 'database_id' filter")
                    payload = {}
                    if "filter" in q.filters:
                        payload["filter"] = q.filters["filter"]
                    if "sorts" in q.filters:
                        payload["sorts"] = q.filters["sorts"]
                    if q.cursor:
                        payload["start_cursor"] = q.cursor
                    if q.limit:
                        payload["page_size"] = min(q.limit, 100)
                    r = await client.post(f"/databases/{database_id}/query", json=payload)
                    r.raise_for_status()
                    body = r.json()
                    records = _safe_records(body, "results")
                    return ConnectorResult(
                        records=records,
                        total=len(records),
                        next_cursor=_safe_cursor(body.get("next_cursor")) if isinstance(body, dict) else None,
                    )

                case "page":
                    page_id = q.filters.get("page_id")
                    if not page_id:
                        raise ValueError("Notion page query requires 'page_id' filter")
                    r = await client.get(f"/pages/{page_id}")
                    r.raise_for_status()
                    body = r.json()
                    return ConnectorResult(records=[body])

                case "blocks":
                    block_id = q.filters.get("block_id")
                    if not block_id:
                        raise ValueError("Notion blocks query requires 'block_id' filter")
                    params = {}
                    if q.cursor:
                        params["start_cursor"] = q.cursor
                    if q.limit:
                        params["page_size"] = str(min(q.limit, 100))
                    r = await client.get(f"/blocks/{block_id}/children", params=params)
                    r.raise_for_status()
                    body = r.json()
                    records = _safe_records(body, "results")
                    return ConnectorResult(
                        records=records,
                        total=len(records),
                        next_cursor=_safe_cursor(body.get("next_cursor")) if isinstance(body, dict) else None,
                    )

                case "users":
                    params = {}
                    if q.cursor:
                        params["start_cursor"] = q.cursor
                    if q.limit:
                        params["page_size"] = str(min(q.limit, 100))
                    r = await client.get("/users", params=params)
                    r.raise_for_status()
                    body = r.json()
                    records = _safe_records(body, "results")
                    return ConnectorResult(
                        records=records,
                        total=len(records),
                        next_cursor=_safe_cursor(body.get("next_cursor")) if isinstance(body, dict) else None,
                    )

                case _:
                    raise ValueError(f"Unsupported Notion resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "page":
                    r = await client.post("/pages", json=payload.data)
                    r.raise_for_status()
                    body: dict[str, Any] = r.json()
                    return body

                case "database":
                    r = await client.post("/databases", json=payload.data)
                    r.raise_for_status()
                    return cast(_DICT_STR_ANY, r.json())

                case "block_append":
                    block_id = payload.data.get("block_id")
                    if not block_id:
                        raise ValueError("Notion block_append requires 'block_id' in data")
                    children = payload.data.get("children", [])
                    r = await client.patch(
                        f"/blocks/{block_id}/children",
                        json={"children": children},
                    )
                    r.raise_for_status()
                    return cast(_DICT_STR_ANY, r.json())

                case "page_update":
                    page_id = payload.data.get("id")
                    if not page_id:
                        raise ValueError("Notion page_update requires 'id' in data")
                    properties = payload.data.get("properties", {})
                    r = await client.patch(
                        f"/pages/{page_id}",
                        json={"properties": properties},
                    )
                    r.raise_for_status()
                    return cast(_DICT_STR_ANY, r.json())

                case _:
                    raise ValueError(f"Unsupported Notion write resource: {payload.resource!r}")
