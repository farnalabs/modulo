"""SharePointConnector — async Microsoft Graph API connector for SharePoint."""

from typing import Any
from urllib.parse import parse_qs, urlparse

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
    health_check_failure,
)
from modulo.core.ssrf import pinned_async_client_sync

# Pagination query parameter name shared across list endpoints (S1192).
_SKIP_TOKEN = "$skiptoken"  # nosec B105 — Graph pagination query-param name, not a credential


class SharePointConnector(ConnectorBase):
    """Read/write SharePoint sites, lists, and files via Microsoft Graph API.

    Uses OAuth 2.0 Bearer token authentication.

    Supported query resources:
      "sites"      — list SharePoint sites; filters: {"search": "..."}
      "lists"      — list lists in a site; filters: {"site_id": "..."}
      "list_items" — get items from a list; filters: {"site_id": "...", "list_id": "..."}
      "drive"      — list files in a drive; filters: {"site_id": "...", "drive_id": "...", "path": "..."}
      "file"       — read file content; filters: {"site_id": "...", "drive_id": "...", "path": "..."}

    Supported write resources:
      "list_item"  — create a list item; data: {"site_id": "...", "list_id": "...", "fields": {...}}
      "file"       — upload/update a file; data: {"site_id": "...", "drive_id": "...", "path": "...", "content": "..."}
    """

    _BASE_URL = "https://graph.microsoft.com/v1.0"

    def _page(
        self,
        body: object,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """Safely extract records, next cursor, and total from a list page.

        ``@odata.nextLink`` pagination cursors are only emitted when the link
        is a non-empty string carrying a non-empty ``$skiptoken``, mirroring
        the microsoft_teams / Snyk cursor-hardening programme.
        """
        records = _safe_records(body, "value")
        next_cursor: str | None = None
        if isinstance(body, dict):
            next_link = body.get("@odata.nextLink", "")
            if isinstance(next_link, str) and next_link:
                skiptoken = parse_qs(urlparse(next_link).query).get(_SKIP_TOKEN, [""])[0]
                next_cursor = _safe_cursor(skiptoken)
        return records[: limit or len(records)], next_cursor, len(records)

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SHAREPOINT

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(
            self._BASE_URL,
            base_url=self._BASE_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the user's SharePoint sites root."""
        try:
            async with self._client() as client:
                r = await client.get("/sites/root")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            site_info = r.json()
            display_name = site_info.get("displayName", "") if isinstance(site_info, dict) else ""
            return HealthResult(ok=True, detail=display_name)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"SharePoint API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="SharePoint API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="SharePoint API connection error")
        except ValueError as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "sites":
                    params: dict[str, Any] = {}
                    search = q.filters.get("search")
                    if search:
                        params["search"] = search
                    if q.limit:
                        params["$top"] = q.limit
                    if q.cursor:
                        params[_SKIP_TOKEN] = q.cursor
                    r = await client.get("/sites", params=params)
                    r.raise_for_status()
                    records, next_cursor, total = self._page(r.json(), q.limit)
                    return ConnectorResult(records=records, next_cursor=next_cursor, total=total)

                case "lists":
                    site_id = q.filters.get("site_id")
                    if not site_id:
                        raise ValueError("SharePoint lists query requires 'site_id' filter")
                    params = {}
                    if q.limit:
                        params["$top"] = q.limit
                    if q.cursor:
                        params[_SKIP_TOKEN] = q.cursor
                    r = await client.get(f"/sites/{site_id}/lists", params=params)
                    r.raise_for_status()
                    records, next_cursor, total = self._page(r.json(), q.limit)
                    return ConnectorResult(records=records, next_cursor=next_cursor, total=total)

                case "list_items":
                    site_id = q.filters.get("site_id")
                    list_id = q.filters.get("list_id")
                    if not site_id or not list_id:
                        raise ValueError("SharePoint list_items query requires 'site_id' and 'list_id' filters")
                    params = {}
                    if q.limit:
                        params["$top"] = q.limit
                    if q.cursor:
                        params[_SKIP_TOKEN] = q.cursor
                    r = await client.get(f"/sites/{site_id}/lists/{list_id}/items", params=params)
                    r.raise_for_status()
                    records, next_cursor, total = self._page(r.json(), q.limit)
                    return ConnectorResult(records=records, next_cursor=next_cursor, total=total)

                case "drive":
                    site_id = q.filters.get("site_id")
                    drive_id = q.filters.get("drive_id")
                    if not site_id or not drive_id:
                        raise ValueError("SharePoint drive query requires 'site_id' and 'drive_id' filters")
                    path = q.filters.get("path", "/")
                    if path == "/" or not path:
                        url = f"/sites/{site_id}/drives/{drive_id}/root/children"
                    else:
                        url = f"/sites/{site_id}/drives/{drive_id}/root:/{path.strip('/')}:/children"
                    params = {}
                    if q.limit:
                        params["$top"] = q.limit
                    if q.cursor:
                        params[_SKIP_TOKEN] = q.cursor
                    r = await client.get(url, params=params)
                    r.raise_for_status()
                    records, next_cursor, total = self._page(r.json(), q.limit)
                    return ConnectorResult(records=records, next_cursor=next_cursor, total=total)

                case "file":
                    site_id = q.filters.get("site_id")
                    drive_id = q.filters.get("drive_id")
                    path = q.filters.get("path")
                    if not site_id or not drive_id or not path:
                        raise ValueError("SharePoint file query requires 'site_id', 'drive_id', and 'path' filters")
                    r = await client.get(f"/sites/{site_id}/drives/{drive_id}/root:/{path.strip('/')}:/content")
                    r.raise_for_status()
                    content = r.text
                    return ConnectorResult(records=[{"path": path, "content": content}])

                case _:
                    raise ValueError(f"Unsupported SharePoint resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "list_item":
                    site_id = payload.data.get("site_id")
                    list_id = payload.data.get("list_id")
                    fields = payload.data.get("fields")
                    if not site_id or not list_id or fields is None:
                        raise ValueError(
                            "SharePoint list_item write requires 'site_id', 'list_id', and 'fields' in data",
                        )
                    r = await client.post(
                        f"/sites/{site_id}/lists/{list_id}/items",
                        json={"fields": fields},
                    )
                    r.raise_for_status()
                    created: dict[str, Any] = r.json()
                    return created

                case "file":
                    site_id = payload.data.get("site_id")
                    drive_id = payload.data.get("drive_id")
                    path = payload.data.get("path")
                    content = payload.data.get("content")
                    if not site_id or not drive_id or not path or content is None:
                        raise ValueError(
                            "SharePoint file write requires 'site_id', 'drive_id', 'path', and 'content' in data",
                        )
                    r = await client.put(
                        f"/sites/{site_id}/drives/{drive_id}/root:/{path.strip('/')}:/content",
                        content=content,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    r.raise_for_status()
                    result: dict[str, Any] = r.json()
                    return result

                case _:
                    raise ValueError(f"Unsupported SharePoint write resource: {payload.resource!r}")
