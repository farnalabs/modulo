"""ConfluenceConnector — async Confluence Cloud REST API v2 connector."""

from typing import Any

import httpx

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


class ConfluenceConnector(ConnectorBase):
    """Read/write Confluence pages, spaces, and content via REST API v2.

    Config (from config_json):
      "instance" — your-domain.atlassian.net/wiki (without https://)

    Credentials (from credentials_ciphertext):
      "email"    — Atlassian account email (for Basic auth)
      "api_token" — Atlassian API token (for Basic auth)
    Or:
      "token"    — Personal Access / Bearer token

    Supported query resources:
      "pages"     — list pages; filters: {"space_id": "...", "limit": 50}
      "page"      — get single page; filters: {"page_id": "..."}
      "spaces"    — list spaces; filters: {"limit": 50, "type": "global"}
      "space"     — get single space; filters: {"space_id": "..."}
      "content"   — CQL search; filters: {"cql": "..."}
      "children"  — get child pages; filters: {"page_id": "..."}
      "labels"    — get page labels; filters: {"page_id": "..."}

    Supported write resources:
      "page"          — create page; data: {"spaceId": "...", "title": "...", "body": {...}}
      "page_update"   — update page; data: {"id": "...", "title": "...", "body": {...}}
      "label"         — add label to page; data: {"page_id": "...", "label": "..."}
    """

    def __init__(self, instance: str, creds: dict[str, str]) -> None:
        self._instance = instance.rstrip("/")
        self._base_url = f"https://{self._instance}"
        self._auth: httpx.Auth | None = None
        self._token: str | None = None

        if "token" in creds:
            self._token = creds["token"]
        elif "email" in creds and "api_token" in creds:
            self._auth = httpx.BasicAuth(username=creds["email"], password=creds["api_token"])
        else:
            raise ValueError(
                "Confluence credentials must contain either 'token' (PAT/Bearer) or 'email' + 'api_token' (Basic auth)",
            )

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.CONFLUENCE

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): validate + resolve the base_url's host
        # synchronously and pin the validated IP onto the transport, so the
        # connection never re-resolves the host at connect time (closes the
        # DNS-rebinding window). ``trust_env=False`` stops a proxy from
        # re-resolving the destination server-side and defeating the pin.
        return pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers=self._headers(),
            auth=self._auth,
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        """Verify connectivity by fetching the current user via /wiki/rest/api/user/current."""
        try:
            async with self._client() as client:
                r = await client.get("/wiki/rest/api/user/current")

            if r.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {r.status_code}: {r.text[:200]}")

            user_info = r.json()
            display_name = user_info.get("displayName", "")

            return HealthResult(ok=True, detail=display_name)
        except httpx.HTTPStatusError as exc:
            return HealthResult(
                ok=False,
                detail=f"Confluence API HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except httpx.TimeoutException:
            return HealthResult(ok=False, detail="Confluence API timeout")
        except httpx.ConnectError:
            return HealthResult(ok=False, detail="Confluence API connection error")
        except ValueError as exc:
            return health_check_failure(exc)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as client:
            match q.resource:
                case "pages":
                    params: dict[str, Any] = {}
                    space_id = q.filters.get("space_id")
                    if space_id:
                        params["spaceId"] = space_id
                    params["limit"] = q.filters.get("limit", q.limit)
                    r = await client.get("/wiki/api/v2/pages", params=params)
                    r.raise_for_status()
                    body: dict[str, Any] = r.json()
                    results: list[dict[str, Any]] = _safe_records(body, "results")
                    return ConnectorResult(records=results)

                case "page":
                    page_id = q.filters.get("page_id")
                    if not page_id:
                        raise ValueError("Confluence page query requires 'page_id' filter")
                    r = await client.get(f"/wiki/api/v2/pages/{page_id}")
                    r.raise_for_status()
                    return ConnectorResult(records=[r.json()])

                case "spaces":
                    params = {}
                    params["limit"] = q.filters.get("limit", q.limit)
                    space_type = q.filters.get("type")
                    if space_type:
                        params["type"] = space_type
                    r = await client.get("/wiki/api/v2/spaces", params=params)
                    r.raise_for_status()
                    body = r.json()
                    results = _safe_records(body, "results")
                    return ConnectorResult(records=results)

                case "space":
                    space_id = q.filters.get("space_id")
                    if not space_id:
                        raise ValueError("Confluence space query requires 'space_id' filter")
                    r = await client.get(f"/wiki/api/v2/spaces/{space_id}")
                    r.raise_for_status()
                    return ConnectorResult(records=[r.json()])

                case "content":
                    cql = q.filters.get("cql")
                    if not cql:
                        raise ValueError("Confluence content query requires 'cql' filter")
                    params = {"cql": cql}
                    r = await client.get("/wiki/rest/api/content/search", params=params)
                    r.raise_for_status()
                    body = r.json()
                    results = _safe_records(body, "results")
                    return ConnectorResult(records=results)

                case "children":
                    page_id = q.filters.get("page_id")
                    if not page_id:
                        raise ValueError("Confluence children query requires 'page_id' filter")
                    r = await client.get(f"/wiki/api/v2/pages/{page_id}/children")
                    r.raise_for_status()
                    body = r.json()
                    results = _safe_records(body, "results")
                    return ConnectorResult(records=results)

                case "labels":
                    page_id = q.filters.get("page_id")
                    if not page_id:
                        raise ValueError("Confluence labels query requires 'page_id' filter")
                    r = await client.get(f"/wiki/api/v2/pages/{page_id}/labels")
                    r.raise_for_status()
                    body = r.json()
                    results = _safe_records(body, "results")
                    return ConnectorResult(records=results)

                case _:
                    raise ValueError(f"Unsupported Confluence resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as client:
            match payload.resource:
                case "page":
                    r = await client.post("/wiki/api/v2/pages", json=payload.data)
                    r.raise_for_status()
                    created: dict[str, Any] = r.json()
                    return created

                case "page_update":
                    page_id = payload.data.get("id")
                    if not page_id:
                        raise ValueError("Confluence page_update requires 'id' in data")
                    r = await client.put(f"/wiki/api/v2/pages/{page_id}", json=payload.data)
                    r.raise_for_status()
                    return {"id": page_id, "updated": True}

                case "label":
                    page_id = payload.data.get("page_id")
                    label = payload.data.get("label")
                    if not page_id or not label:
                        raise ValueError("Confluence label requires 'page_id' and 'label' in data")
                    r = await client.post(
                        f"/wiki/api/v2/pages/{page_id}/labels",
                        json={"name": label},
                    )
                    r.raise_for_status()
                    return {"page_id": page_id, "label": label, "created": True}

                case _:
                    raise ValueError(f"Unsupported Confluence write resource: {payload.resource!r}")
