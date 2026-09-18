"""DropboxPaperConnector — async Dropbox Paper API v2 connector."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

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

_DROPBOX_API = "https://api.dropboxapi.com/2"


class DropboxPaperConnector(ConnectorBase):
    """Read/write Dropbox Paper documents via the Dropbox API v2.

    Credentials (from credentials_ciphertext):
      "token" — Dropbox OAuth 2.0 access token (Bearer token)

    Supported query resources:
      "docs"      — POST /paper/docs/list; filters: {"filter_by": "...", "sort_by": "...", "sort_order": "..."}
      "doc"       — POST /paper/docs/download; filters: {"doc_id": "..."}
      "folders"   — POST /files/list_folder; filters: {"path": "...", "recursive": bool}

    Supported write resources:
      "doc"       — POST /paper/docs/create with import_format=markdown
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.DROPBOX_PAPER

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
        }

    def _client(self) -> httpx.AsyncClient:
        return pinned_async_client_sync(
            _DROPBOX_API,
            base_url=_DROPBOX_API,
            headers=self._headers(),
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the current account."""
        try:
            async with self._client() as client:
                r = await client.post("/users/get_current_account", json=None)

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            body: dict[str, Any] = r.json()
            email = body.get("email", "unknown")
            return HealthResult(ok=True, detail=f"Authenticated as {email}")
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Dropbox API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Dropbox API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Dropbox API connection error")
        except ValueError as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            handler = self._query_handlers().get(q.resource)
            if handler is None:
                raise ValueError(f"Unsupported Dropbox Paper resource: {q.resource!r}")
            return await handler(client, q)

    def _query_handlers(self) -> dict[str, Callable[[httpx.AsyncClient, ConnectorQuery], Awaitable[ConnectorResult]]]:
        return {
            "docs": self._query_docs,
            "doc": self._query_doc,
            "folders": self._query_folders,
        }

    async def _query_docs(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        payload: dict[str, Any] = self._docs_payload(q)
        r = await client.post("/paper/docs/list", json=payload)
        r.raise_for_status()
        body = r.json()
        records: list[dict[str, Any]] = [{"doc_id": did} for did in _safe_records(body, "doc_ids")]
        next_cursor: str | None = self._docs_next_cursor(body)
        return ConnectorResult(records=records, total=len(records), next_cursor=next_cursor)

    async def _query_doc(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        doc_id = q.filters.get("doc_id")
        if not doc_id:
            raise ValueError("Dropbox Paper doc query requires 'doc_id' filter")
        r = await client.post(
            "/paper/docs/download",
            headers={"Dropbox-API-Arg": json.dumps({"doc_id": doc_id})},
            content=b"",
        )
        r.raise_for_status()
        return ConnectorResult(records=[{"doc_id": doc_id, "content": r.text}], total=1)

    async def _query_folders(self, client: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        payload = {
            "path": q.filters.get("path", ""),
            "recursive": q.filters.get("recursive", False),
        }
        self._apply_pagination(payload, q)
        r = await client.post("/files/list_folder", json=payload)
        r.raise_for_status()
        body = r.json()
        entries = _safe_records(body, "entries")
        next_cursor = self._folders_next_cursor(body)
        return ConnectorResult(records=entries, total=len(entries), next_cursor=next_cursor)

    @staticmethod
    def _apply_pagination(payload: dict[str, Any], q: ConnectorQuery) -> None:
        if q.cursor:
            payload["cursor"] = q.cursor

    @staticmethod
    def _docs_payload(q: ConnectorQuery) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "limit": min(q.limit, 100),
            "filter_by": q.filters.get("filter_by", "docs_created"),
        }
        sort_by = q.filters.get("sort_by")
        if sort_by:
            payload["sort_by"] = sort_by
        sort_order = q.filters.get("sort_order")
        if sort_order:
            payload["sort_order"] = sort_order
        DropboxPaperConnector._apply_pagination(payload, q)
        return payload

    @staticmethod
    def _docs_next_cursor(body: Any) -> str | None:
        cursor_obj = body.get("cursor") if isinstance(body, dict) else None
        return _safe_cursor(cursor_obj.get("value")) if isinstance(cursor_obj, dict) else None

    @staticmethod
    def _folders_next_cursor(body: Any) -> str | None:
        cursor = body.get("cursor") if isinstance(body, dict) else None
        return _safe_cursor(cursor.get("value")) if isinstance(cursor, dict) else _safe_cursor(cursor)

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "doc":
                    title = payload.data.get("title", "Untitled")
                    content = payload.data.get("content", "")
                    r = await client.post(
                        "/paper/docs/create",
                        params={"import_format": "markdown"},
                        headers={"Dropbox-API-Arg": json.dumps({"path": f"/{title}"})},
                        content=content.encode("utf-8"),
                    )
                    r.raise_for_status()
                    result_str = r.headers.get("Dropbox-API-Result", "{}")
                    body: dict[str, Any] = json.loads(result_str)
                    return body

                case _:
                    raise ValueError(f"Unsupported Dropbox Paper write resource: {payload.resource!r}")
